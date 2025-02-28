from abc import ABCMeta, abstractmethod

import torch
from mgds.MGDS import MGDS, TrainDataLoader

from modules.dataLoader.mixin.DataLoaderMgdsMixin import DataLoaderMgdsMixin
from modules.model.BaseModel import BaseModel
from modules.util.TrainProgress import TrainProgress
from modules.util.args.TrainArgs import TrainArgs


class BaseDataLoader(
    DataLoaderMgdsMixin,
    metaclass=ABCMeta,
):

    def __init__(
            self,
            train_device: torch.device,
            temp_device: torch.device,
    ):
        super(BaseDataLoader, self).__init__()

        self.train_device = train_device
        self.temp_device = temp_device

    @abstractmethod
    def get_data_set(self) -> MGDS:
        pass

    @abstractmethod
    def get_data_loader(self) -> TrainDataLoader:
        pass

    @abstractmethod
    def _setup_cache_device(
            self,
            model: BaseModel,
            train_device: torch.device,
            temp_device: torch.device,
            args: TrainArgs,
    ):
        pass
