"""Adapted from: https://github.com/facebookresearch/moco.
Original work is: Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
"""
from argparse import ArgumentParser
from typing import Union, Any, Dict, List, Tuple, Type
import os

import torch
import torch.distributed
from torch import Tensor, nn
from torch.nn import functional
from torch.nn.functional import softmax
from torch.optim import Adam
from torch.optim.optimizer import Optimizer
from torchmetrics.functional import accuracy
from pytorch_lightning.plugins import DDP2Plugin, DDPPlugin
from pytorch_lightning import LightningModule, Trainer, seed_everything

from pl_bolts.metrics import mean, precision_at_k
from pl_bolts.models.self_supervised.moco.transforms import (
    Moco2EvalCIFAR10Transforms,
    Moco2EvalImagenetTransforms,
    Moco2EvalSTL10Transforms,
    Moco2TrainCIFAR10Transforms,
    Moco2TrainImagenetTransforms,
    Moco2TrainSTL10Transforms,
)
from pl_bolts.utils import _TORCHVISION_AVAILABLE
from pl_bolts.utils.warnings import warn_missing_pkg

if _TORCHVISION_AVAILABLE:
    import torchvision
else:  # pragma: no cover
    warn_missing_pkg("torchvision")

from pytorch_lightning.loggers import WandbLogger
from pytorch_lightning.callbacks import ModelCheckpoint, EarlyStopping
from pykeops.torch import LazyTensor


class MDEE(LightningModule):
    """PyTorch Lightning implementation of `Moco <https://arxiv.org/abs/2003.04297>`_
    Paper authors: Xinlei Chen, Haoqi Fan, Ross Girshick, Kaiming He.
    Code adapted from `facebookresearch/moco <https://github.com/facebookresearch/moco>`_ to Lightning by:
    Example::
        from pl_bolts.models.self_supervised import Moco_v2
        model = Moco_v2()
        trainer = Trainer()
        trainer.fit(model)
    CLI command::
        # cifar10
        python moco2_module.py --gpus 1
        # imagenet
        python moco2_module.py
            --gpus 8
            --dataset imagenet2012
            --data_dir /path/to/imagenet/
            --meta_dir /path/to/folder/with/meta.bin/
            --batch_size 32
    """

    def __init__(
            self,
            base_encoder: Union[str, torch.nn.Module] = 'resnet18',
            emb_dim: int = 128,
            num_negatives: int = 65536,
            encoder_momentum: float = 0.999,
            softmax_temperature: float = 0.07,
            learning_rate: float = 0.03,
            momentum: float = 0.9,
            weight_decay: float = 1e-4,
            data_dir: str = './',
            batch_size: int = 256,
            num_workers: int = 64,
            use_mlp: bool = True,
            target_categories: int = 10,
            use_knn: bool = False,
            use_kmeans: bool = False,
            alpha: float = 0.1,
            topk: int = 500,
            metric: str = "euclidean",
            *args,
            **kwargs
    ):
        """
        Args:
            base_encoder: torchvision model name or torch.nn.Module
            emb_dim: feature dimension (default: 128)
            num_negatives: queue size; number of negative keys (default: 65536)
            encoder_momentum: moco momentum of updating key encoder (default: 0.999)
            softmax_temperature: softmax temperature (default: 0.07)
            learning_rate: the learning rate
            momentum: optimizer momentum
            weight_decay: optimizer weight decay
            datamodule: the DataModule (train, val, test dataloaders)
            data_dir: the directory to store data
            batch_size: batch size
            use_mlp: add an mlp to the encoders
            num_workers: workers for the loaders
        """

        super().__init__()
        self.save_hyperparameters()

        # create the encoders
        # num_classes is the output fc dimension
        self.encoder_q, self.encoder_k = self.init_encoders(base_encoder)

        if use_mlp:  # hack: brute-force replacement
            dim_mlp = self.encoder_q.fc.weight.shape[1]
            self.encoder_q.fc = nn.Sequential(nn.Linear(dim_mlp, dim_mlp), nn.ReLU(), self.encoder_q.fc)
            self.encoder_k.fc = nn.Sequential(nn.Linear(dim_mlp, dim_mlp), nn.ReLU(), self.encoder_k.fc)

        for param_q, param_k in zip(self.encoder_q.parameters(), self.encoder_k.parameters()):
            param_k.data.copy_(param_q.data)  # initialize
            param_k.requires_grad = False  # not update by gradient

        self.use_knn = use_knn
        if self.use_knn:
            self.topk = topk
            self.metric = metric

        self.use_kmeans_loss = use_kmeans
        if self.use_kmeans_loss:
            self.encoder_kmeans = nn.Sequential(nn.Linear(emb_dim, target_categories))

        # create the queue
        self.register_buffer("queue", torch.randn(emb_dim, num_negatives))
        self.queue = nn.functional.normalize(self.queue, dim=0)

        self.register_buffer("queue_ptr", torch.zeros(1, dtype=torch.long))

        # create the validation queue
        self.register_buffer("val_queue", torch.randn(emb_dim, num_negatives))
        self.val_queue = nn.functional.normalize(self.val_queue, dim=0)

        self.register_buffer("val_queue_ptr", torch.zeros(1, dtype=torch.long))

    def init_encoders(self, base_encoder):
        """Override to add your own encoders."""

        template_model = getattr(torchvision.models, base_encoder)
        encoder_q = template_model(num_classes=self.hparams.emb_dim)
        encoder_k = template_model(num_classes=self.hparams.emb_dim)

        return encoder_q, encoder_k

    @torch.no_grad()
    def _momentum_update_key_encoder(self):
        """Momentum update of the key encoder."""
        for param_q, param_k in zip(self.encoder_q.parameters(), self.encoder_k.parameters()):
            em = self.hparams.encoder_momentum
            param_k.data = param_k.data * em + param_q.data * (1.0 - em)

    @torch.no_grad()
    def _dequeue_and_enqueue(self, keys, queue_ptr, queue):
        # gather keys before updating queue
        if self._use_ddp_or_ddp2(self.trainer):
            keys = concat_all_gather(keys)

        batch_size = keys.shape[0]

        ptr = int(queue_ptr)
        # assert self.hparams.num_negatives % batch_size == 0  # for simplicity

        if batch_size == self.hparams.batch_size:
            # replace the keys at ptr (dequeue and enqueue)
            queue[:, ptr: ptr + batch_size] = keys.T
            ptr = (ptr + batch_size) % self.hparams.num_negatives  # move pointer
            queue_ptr[0] = ptr

    @torch.no_grad()
    def _batch_shuffle_ddp(self, x):  # pragma: no cover
        """Batch shuffle, for making use of BatchNorm.
        *** Only support DistributedDataParallel (DDP) model. ***
        """
        # gather from all gpus
        batch_size_this = x.shape[0]
        x_gather = concat_all_gather(x)
        batch_size_all = x_gather.shape[0]

        num_gpus = batch_size_all // batch_size_this

        # random shuffle index
        idx_shuffle = torch.randperm(batch_size_all).cuda()

        # broadcast to all gpus
        torch.distributed.broadcast(idx_shuffle, src=0)

        # index for restoring
        idx_unshuffle = torch.argsort(idx_shuffle)

        # shuffled index for this gpu
        gpu_idx = torch.distributed.get_rank()
        idx_this = idx_shuffle.view(num_gpus, -1)[gpu_idx]

        return x_gather[idx_this], idx_unshuffle

    @torch.no_grad()
    def _batch_unshuffle_ddp(self, x, idx_unshuffle):  # pragma: no cover
        """Undo batch shuffle.
        *** Only support DistributedDataParallel (DDP) model. ***
        """
        # gather from all gpus
        batch_size_this = x.shape[0]
        x_gather = concat_all_gather(x)
        batch_size_all = x_gather.shape[0]

        num_gpus = batch_size_all // batch_size_this

        # restored index for this gpu
        gpu_idx = torch.distributed.get_rank()
        idx_this = idx_unshuffle.view(num_gpus, -1)[gpu_idx]

        return x_gather[idx_this]

    def forward(self, img_q, img_k, queue):
        """
        Input:
            im_q: a batch of query images
            im_k: a batch of key images
            queue: a queue from which to pick negative samples
        Output:
            logits, targets
        """

        # compute query features
        query = self.encoder_q(img_q)  # queries: NxC
        query = nn.functional.normalize(query, dim=1)

        # compute key features
        with torch.no_grad():  # no gradient to keys

            # shuffle for making use of BN
            if self._use_ddp_or_ddp2(self.trainer):
                img_k, idx_unshuffle = self._batch_shuffle_ddp(img_k)

            key = self.encoder_k(img_k)  # keys: NxC
            key = nn.functional.normalize(key, dim=1)

            # undo shuffle
            if self._use_ddp_or_ddp2(self.trainer):
                key = self._batch_unshuffle_ddp(key, idx_unshuffle)

        # compute logits
        # Einstein sum is more intuitive
        # positive logits: Nx1
        l_pos = torch.einsum("nc,nc->n", [query, key]).unsqueeze(-1)

        # negative logits: NxK
        if self.use_knn:
            neg_examples = self.knn_approx(query)
            l_neg = torch.einsum('nc,njc->nc', [query, neg_examples])
        else:
            neg_examples = queue.clone().detach()
            l_neg = torch.einsum("nc,ck->nk", [query, neg_examples])

        # logits: Nx(1+K)
        logits = torch.cat([l_pos, l_neg], dim=1)

        # apply temperature
        logits /= self.hparams.softmax_temperature

        # labels: positive key indicators
        labels = torch.zeros(logits.shape[0], dtype=torch.long)
        labels = labels.type_as(logits)

        return logits, labels, query, key

    def knn_approx(self, query):
        # Encoding as KeOps LazyTensors:
        queue = torch.transpose(self.queue.clone().detach(), 0, 1)

        X_i = LazyTensor(query[:, None, :].contiguous())  # (256, 1, 128) query set
        X_j = LazyTensor(queue[None, :, :].contiguous())  # (1, 65536, 128) queue set

        # Symbolic distance matrix:
        if self.metric == "euclidean":
            D_ij = ((X_i - X_j) ** 2).sum(-1)
        elif self.metric == "manhattan":
            D_ij = (X_i - X_j).abs().sum(-1)
        elif self.metric == "angular":
            D_ij = -(X_i | X_j)
        elif self.metric == "hyperbolic":
            D_ij = ((X_i - X_j) ** 2).sum(-1) / (X_i[0] * X_j[0])  # symbolic matrix of distances
        elif self.metric == "ang+hyper":
            D_ij = -(X_i | X_j) + ((X_i - X_j) ** 2).sum(-1) / (X_i[0] * X_j[0])

        # K-NN query:
        index_knn = D_ij.argKmin(self.topk, dim=1)  # Samples <-> Dataset, (N_test, K)
        knn_queue = queue[index_knn, :]  # k nearest neighbors

        return knn_queue

    def kmeans_loss(self, query, key):
        kmeans_query = self.encoder_kmeans(torch.nn.functional.normalize(query))
        kmeans_key = self.encoder_kmeans(torch.nn.functional.normalize(key))
        # torch.sqrt(torch.tensor(self.hparams.target_categories))

        kmeans_query_labels = nn.Softmax(dim=1)(self.hparams.alpha * kmeans_query)
        kmeans_key_labels = nn.Softmax(dim=1)(self.hparams.alpha * kmeans_key)

        loss = -((kmeans_query_labels * kmeans_query).sum() + (kmeans_key_labels * kmeans_key).sum()) / self.hparams.alpha

        return loss

    def training_step(self, batch, batch_idx):
        # in STL10 we pass in both lab+unl for online ft
        if self.trainer.datamodule.name == "stl10":
            # labeled_batch = batch[1]
            unlabeled_batch = batch[0]
            batch = unlabeled_batch

        (img_1, img_2), _ = batch

        self._momentum_update_key_encoder()  # update the key encoder
        output, target, query, keys = self(img_q=img_1, img_k=img_2, queue=self.queue)
        self._dequeue_and_enqueue(keys, queue=self.queue, queue_ptr=self.queue_ptr)  # dequeue and enqueue

        loss = functional.cross_entropy(output.float(), target.long())

        if self.use_kmeans_loss:
            loss += self.kmeans_loss(query, keys)

        acc1, acc5 = precision_at_k(output, target, top_k=(1, 5))

        log = {"train_loss": loss, "train_acc1": acc1, "train_acc5": acc5}
        self.log_dict(log)
        return loss

    def validation_step(self, batch, batch_idx):
        # in STL10 we pass in both lab+unl for online ft
        if self.trainer.datamodule.name == "stl10":
            # labeled_batch = batch[1]
            unlabeled_batch = batch[0]
            batch = unlabeled_batch

        (img_1, img_2), labels = batch

        output, target, query, keys = self(img_q=img_1, img_k=img_2, queue=self.val_queue)
        self._dequeue_and_enqueue(keys, queue=self.val_queue, queue_ptr=self.val_queue_ptr)  # dequeue and enqueue

        loss = functional.cross_entropy(output, target.long())

        if self.use_kmeans_loss:
            loss += self.kmeans_loss(query, keys)

        acc1, acc5 = precision_at_k(output, target, top_k=(1, 5))

        results = {"val_loss": loss, "val_acc1": acc1, "val_acc5": acc5}
        # self.log_dict(results)
        return results

    def validation_epoch_end(self, outputs):
        val_loss = mean(outputs, "val_loss")
        val_acc1 = mean(outputs, "val_acc1")
        val_acc5 = mean(outputs, "val_acc5")

        log = {"val_loss": val_loss, "val_acc1": val_acc1, "val_acc5": val_acc5}
        self.log_dict(log)

    def configure_optimizers(self):
        optimizer = torch.optim.SGD(
            self.parameters(),
            self.hparams.learning_rate,
            momentum=self.hparams.momentum,
            weight_decay=self.hparams.weight_decay,
        )
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
            optimizer,
            self.trainer.max_epochs,
        )
        return [optimizer], [scheduler]

    @staticmethod
    def add_model_specific_args(parent_parser):
        parser = ArgumentParser(parents=[parent_parser], add_help=False)
        parser.add_argument("--base_encoder", type=str, default="resnet18")
        parser.add_argument("--emb_dim", type=int, default=128)
        parser.add_argument("--num_workers", type=int, default=64)
        parser.add_argument("--num_negatives", type=int, default=65536)
        parser.add_argument("--encoder_momentum", type=float, default=0.999)
        parser.add_argument("--softmax_temperature", type=float, default=0.07)
        parser.add_argument("--learning_rate", type=float, default=0.03)
        parser.add_argument("--momentum", type=float, default=0.9)
        parser.add_argument("--weight_decay", type=float, default=1e-4)
        parser.add_argument("--data_dir", type=str, default="./")
        parser.add_argument("--dataset", type=str, default="cifar10", choices=["cifar10", "imagenet2012", "stl10"])
        parser.add_argument("--batch_size", type=int, default=256)
        parser.add_argument("--use_mlp", action="store_true")
        parser.add_argument("--meta_dir", default=".", type=str, help="path to meta.bin for imagenet")

        return parser

    @staticmethod
    def _use_ddp_or_ddp2(trainer: Trainer) -> bool:
        return isinstance(trainer.training_type_plugin, (DDPPlugin, DDP2Plugin))


# utils
@torch.no_grad()
def concat_all_gather(tensor):
    """Performs all_gather operation on the provided tensors.
    *** Warning ***: torch.distributed.all_gather has no gradient.
    """
    tensors_gather = [torch.ones_like(tensor) for _ in range(torch.distributed.get_world_size())]
    torch.distributed.all_gather(tensors_gather, tensor, async_op=False)

    output = torch.cat(tensors_gather, dim=0)
    return output


def cli_main():
    from pl_bolts.datamodules import CIFAR10DataModule, SSLImagenetDataModule, STL10DataModule

    parser = ArgumentParser()

    # trainer args
    parser = Trainer.add_argparse_args(parser)

    # model args
    parser = MDEE.add_model_specific_args(parser)
    args = parser.parse_args()

    if args.dataset == "cifar10":
        datamodule = CIFAR10DataModule.from_argparse_args(args)
        datamodule.train_transforms = Moco2TrainCIFAR10Transforms()
        datamodule.val_transforms = Moco2EvalCIFAR10Transforms()

    elif args.dataset == "stl10":
        datamodule = STL10DataModule.from_argparse_args(args)
        datamodule.train_dataloader = datamodule.train_dataloader_mixed
        datamodule.val_dataloader = datamodule.val_dataloader_mixed
        datamodule.train_transforms = Moco2TrainSTL10Transforms()
        datamodule.val_transforms = Moco2EvalSTL10Transforms()

    elif args.dataset == "imagenet2012":
        datamodule = SSLImagenetDataModule.from_argparse_args(args)
        datamodule.train_transforms = Moco2TrainImagenetTransforms()
        datamodule.val_transforms = Moco2EvalImagenetTransforms()

    else:
        # replace with your own dataset, otherwise CIFAR-10 will be used by default if `None` passed in
        datamodule = None

    model = MDEE(**args.__dict__)
    run_name = 'KMeans'
    current_project = 'MDEE'
    wandb_logger = WandbLogger(name=run_name, project=current_project)

    checkpoint_callback = ModelCheckpoint(monitor='val_loss', dirpath=os.path.join('./', current_project, args.dataset),
                                          filename=run_name)
    trainer = Trainer.from_argparse_args(args, logger=wandb_logger, callbacks=[checkpoint_callback],
                                         benchmark=True, max_epochs=10)
    trainer.fit(model, datamodule=datamodule)

    if torch.cuda.is_available():
        torch.cuda.empty_cache()


if __name__ == "__main__":
    cli_main()