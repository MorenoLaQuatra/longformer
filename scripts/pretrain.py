import argparse
import glob
import os
import random
import logging
import numpy as np
from tqdm import tqdm
import torch
from transformers import AutoTokenizer, AutoModelWithLMHead
from transformers import DataCollatorForLanguageModeling
from transformers.optimization import AdamW, get_linear_schedule_with_warmup

from torch.utils.data import Dataset, DataLoader
import pytorch_lightning as ptl
from pytorch_lightning.logging.test_tube import TestTubeLogger
from pytorch_lightning.callbacks import ModelCheckpoint


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class MMapTextDataset(Dataset):
    def __init__(self, mmap_filename, chunk_size):
        self.num_instances = np.memmap(mmap_filename, mode='r', dtype=np.uint16).shape[0] // chunk_size
        # defer loading the token_ids memmap until after the first __getitem__ call.
        # when spawning new processes for ddp, there is a hard limit in python < 3.8 that
        # pickle files need to be < 4GB. By waiting until after the first __getitem__ we
        # don't have to pickle the memmap
        self.token_ids = None
        self._mmap_filename = mmap_filename
        self._chunk_size = chunk_size

    def __len__(self):
        return self.num_instances

    def __getitem__(self, i):
        if self.token_ids is None:
            self.token_ids = np.memmap(self._mmap_filename, mode='r', dtype=np.uint16,
                                       shape=(self.num_instances, self._chunk_size))
        return torch.tensor(self.token_ids[i, :].astype(np.int32), dtype=torch.long)

    @staticmethod
    def raw_text_to_mmap(args):
        tokenizer = AutoTokenizer.from_pretrained(args.tokenizer)
        assert len(tokenizer) < 65535  # will use uint16 to store token ids
        all_files = glob.glob(f'{args.input_dir}/*.txt')

        if os.path.exists(f'{args.input_dir}/cache/'):
            logger.info("Cache already exists. Remove the cache directory to regenerate")
            return
        os.mkdir(f'{args.input_dir}/cache/')
        train_chunks = []
        val_chunks = []

        # TODO: process each shared in a separate worker
        # TODO: support multiple documents in one chunk instead of padding
        for fname in tqdm(all_files):
            with open(fname, 'r') as fin:
                for line in tqdm(fin):
                    if line.strip() == '':  # drop empty lines
                        continue
                    chunks_list = train_chunks if random.random() > args.train_dev_split else val_chunks
                    tokens = tokenizer.tokenize(line)  # each line is one document
                    # generate chunks of length args.seqlen. The last chunk will be padded.
                    # padding last chunk is not great for longformer because many chunks will be mostly padding
                    current_chunk = [tokenizer.bos_token]
                    for token in tokens:
                        if len(current_chunk) == args.seqlen - 1:  # chunk is full
                            current_chunk.append(tokenizer.eos_token)
                            chunks_list.append(current_chunk)
                            current_chunk = [tokenizer.bos_token]
                        current_chunk.append(token)
                    current_chunk.extend([tokenizer.pad_token] * (args.seqlen - len(current_chunk)))
                    current_chunk[args.seqlen - 1] = tokenizer.eos_token
                    chunks_list.append(current_chunk)

        def _tokenized_text_to_mmap(output_fname, chunks_list):
            random.shuffle(chunks_list)
            num_chunks = len(chunks_list)
            all_token_ids = np.empty((num_chunks, args.seqlen), dtype=np.uint16)
            for k, chunk in enumerate(tqdm(chunks_list)):
                token_ids = tokenizer.convert_tokens_to_ids(chunk)
                assert len(token_ids) == args.seqlen
                all_token_ids[k, :] = [int(t) for t in token_ids]
            fp = np.memmap(output_fname, dtype=np.uint16, mode='w+', shape=(num_chunks, args.seqlen))
            fp[:, :] = all_token_ids[:, :]
            fp.flush()
            del fp

        _tokenized_text_to_mmap(f'{args.input_dir}/cache/train.bin', train_chunks)
        _tokenized_text_to_mmap(f'{args.input_dir}/cache/val.bin', val_chunks)


class Pretrainer(ptl.LightningModule):

    def __init__(self, hparams):
        super().__init__()

        self.args = hparams
        self.hparams = self.args

        self.model = AutoModelWithLMHead.from_pretrained(args.model)
        self.config = self.model.config
        tokenizer = AutoTokenizer.from_pretrained(args.tokenizer)
        self.pad_token_id = tokenizer.pad_token_id

        logger.info(f'Creating dataset cache from dir {self.args.input_dir}. This could be slow the first time.')
        MMapTextDataset.raw_text_to_mmap(args)

        # TODO: add support for other objective functions
        self.data_collator = DataCollatorForLanguageModeling(
            tokenizer=tokenizer, mlm=True, mlm_probability=self.args.mlm_prob
        )

    def forward(self, input_ids=None, labels=None, loss_only=True):
        # get the padding mask - 1 for NOT masked, 0 for MASKED/PAD
        attention_mask = (input_ids != self.pad_token_id).int()

        if labels is not None:
            # output is loss, prediction_scores, hidden_states
            output = self.model(input_ids=input_ids, attention_mask=attention_mask, masked_lm_labels=labels)
            if loss_only:
                return output[0]
            else:
                return {"loss": output[0], "hidden_states": output[2]}
        else:
            # don't need to run the lm_head
            assert not loss_only
            output = self.model.roberta(input_ids=input_ids, attention_mask=attention_mask)
            return {"hidden_states": output[2]}

    def training_step(self, batch, batch_nb):
        loss = self(**batch)
        tensorboard_logs = {
            'mlm_loss': loss.detach(),
            'mlm_perplexity': torch.exp(loss).detach(),
        }
        return {'loss': loss, 'log': tensorboard_logs}

    def validation_step(self, batch, batch_nb):
        loss = self(**batch)
        tensorboard_logs = {
            'val_mlm_loss': loss.detach(),
        }
        return {'val_loss': tensorboard_logs["val_mlm_loss"], 'log': tensorboard_logs}

    def validation_epoch_end(self, outputs):
        avg_loss = torch.stack([x['log']['val_mlm_loss'] for x in outputs if 'val_mlm_loss' in x['log']]).mean()
        if self.use_ddp:
            avg_loss = torch.distributed.all_reduce(avg_loss, op=torch.distributed.ReduceOp.SUM)
            avg_loss /= torch.distributed.get_world_size()
        avg_loss = avg_loss.item()
        logs = {'val_mlm_loss': avg_loss}
        return {'log': logs, 'progress_bar': logs, "val_loss": avg_loss}

    def configure_optimizers(self):
        no_decay = ["bias", "LayerNorm.weight"]

        optimizer_grouped_parameters = [
            {
                "params": [p for n, p in self.named_parameters() if not any(nd in n for nd in no_decay) and p.requires_grad],
                "weight_decay": self.args.weight_decay,
            },
            {
                "params": [p for n, p in self.named_parameters() if any(nd in n for nd in no_decay) and p.requires_grad],
                "weight_decay": 0.0,
            },
        ]
        optimizer = AdamW(optimizer_grouped_parameters, lr=self.args.learning_rate, eps=self.args.adam_epsilon)
        scheduler = get_linear_schedule_with_warmup(
            optimizer, num_warmup_steps=self.args.warmup_steps, num_training_steps=self.args.training_steps
        )

        return [optimizer], [{"scheduler": scheduler, "interval": "step"}]

    def _get_loader(self, fname, is_train):
        dataset = MMapTextDataset(fname, chunk_size=self.args.seqlen)

        if self.trainer.use_ddp:
            sampler = torch.utils.data.distributed.DistributedSampler(dataset, shuffle=is_train)
            shuffle = False
        else:
            sampler = None
            shuffle = is_train

        loader = DataLoader(
                dataset,
                batch_size=self.args.batch_size,
                shuffle=shuffle,
                sampler=sampler,
                num_workers=self.args.num_workers,
                collate_fn=self.data_collator,
                drop_last=is_train,
        )
        return loader

    def train_dataloader(self):
        return self._get_loader(f'{self.args.input_dir}/cache/train.bin', True)

    def val_dataloader(self):
        return self._get_loader(f'{self.args.input_dir}/cache/val.bin', False)

    @staticmethod
    def add_args(parser):
        parser.add_argument("--seed", type=int, default=3)
        parser.add_argument("--input_dir", type=str, required=True)
        parser.add_argument("--save_dir", type=str, default='runs/')
        parser.add_argument("--save_prefix", type=str, required=True)
        parser.add_argument("--train_dev_split", type=float, default=0.05)
        parser.add_argument("--seqlen", type=int, default=512)
        parser.add_argument("--tokenizer", type=str, default='roberta-base')
        parser.add_argument("--model", type=str, default='roberta-base')
        parser.add_argument("--mlm_prob", type=float, default=0.15)
        parser.add_argument("--weight_decay", type=float, default=0.01)
        parser.add_argument("--learning_rate", type=float, default=1e-5)
        parser.add_argument("--adam_epsilon", type=float, default=1e-6)
        parser.add_argument("--training_steps", type=int, default=0.01)
        parser.add_argument("--warmup_steps", type=int, default=1000)
        parser.add_argument("--batch_size", type=int, default=8)
        parser.add_argument("--num_workers", type=int, default=0)
        parser.add_argument("--grad_accum", type=int, default=1)
        parser.add_argument("--gpus", type=str, default='0')
        parser.add_argument("--resume", type=str, default=None)
        parser.add_argument("--num_tpu_cores", type=int, default=None)

        return parser


def main(args):
    random.seed(args.seed * 10)
    np.random.seed(args.seed * 100)
    torch.manual_seed(args.seed * 1000)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed * 10000)

    pretrainer = Pretrainer(args)

    # logger here is a SummaryWritter for tensorboard
    # it is used by the trainer, and certain return variables
    # from the model are automatically logged
    logger = TestTubeLogger(
        save_dir=args.save_dir,
        name=args.save_prefix,
        version=0  # always use version=0
    )

    checkpoint_callback = ModelCheckpoint(
        # model saved to filepath/prefix_....
        filepath=os.path.join(args.save_dir, args.save_prefix, 'checkpoint'),
        prefix='',
        save_top_k=3,
        verbose=True,
        monitor='val_loss',
        mode='min',
    )

    args.gpus = [int(x) for x in args.gpus.split(',')]
    trainer = ptl.Trainer(
        gpus=args.gpus,
        num_tpu_cores=args.num_tpu_cores,
        distributed_backend='ddp' if len(args.gpus) > 1 else None,
        track_grad_norm=-1,
        max_epochs=10000, min_epochs=0, max_steps=args.training_steps,  # run for many epochs, but stop after max_steps
        early_stop_callback=None,
        row_log_interval=25,
        logger=logger,
        checkpoint_callback=checkpoint_callback,
        resume_from_checkpoint=args.resume,
    )
    trainer.fit(pretrainer)


if __name__ == "__main__":
    parser = Pretrainer.add_args(argparse.ArgumentParser(description="pretrain"))
    args = parser.parse_args()
    main(args)