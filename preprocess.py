""""""
Convolutional Transformer for EEG decoding.

Couples CNN and Transformer in a concise manner for EEG decoding, 
with reported amazing results.
"""

EEG Conformer 

Convolutional Transformer for EEG decoding

Couple CNN and Transformer in a concise manner with amazing results
"""
# remember to change paths

import argparse
import os
gpus = [0]
os.environ['CUDA_DEVICE_ORDER'] = 'PCI_BUS_ID'
os.environ["CUDA_VISIBLE_DEVICES"] = ','.join(map(str, gpus))
import numpy as np
import math     
import glob
import random
import itertools
import datetime
import time
import datetime
import sys
import scipy.io
import h5py

import torchvision.transforms as transforms
from torchvision.utils import save_image, make_grid

from torch.utils.data import DataLoader
from torch.autograd import Variable
# from torchsummary import summary
import torch.autograd as autograd
from torchvision.models import vgg19

import torch.nn as nn
import torch.nn.functional as F
import torch
import torch.nn.init as init

from torch.utils.data import Dataset
from sklearn.decomposition import PCA

import torch
import torch.nn.functional as F
import matplotlib.pyplot as plt

from torch import nn
from torch import Tensor
from PIL import Image
from torchvision.transforms import Compose, Resize, ToTensor
from einops import rearrange, reduce, repeat
from einops.layers.torch import Rearrange, Reduce
# from common_spatial_pattern import csp

import matplotlib.pyplot as plt
# from torch.utils.tensorboard import SummaryWriter
from torch.backends import cudnn
# cudnn.benchmark = False
# cudnn.deterministic = True

# writer = SummaryWriter('./TensorBoardX/')


# Convolution module
# use conv to capture local features, instead of postion embedding.
class PatchEmbedding(nn.Module):
    def __init__(self, emb_size=10):
        # self.patch_size = patch_size
        super().__init__()

        self.shallownet = nn.Sequential(
            nn.Conv2d(102, 10, (1, 25), (1, 1)),
            nn.Conv2d(10, 10, (1, 22), (1, 1)),
            nn.BatchNorm2d(10),
            nn.ELU(),
            nn.AvgPool2d((1, 75), (1, 15)),  # pooling acts as slicing to obtain 'patch' along the time dimension as in ViT
            nn.Dropout(0.5),
        )

        self.projection = nn.Sequential(
            nn.Conv2d(10, emb_size, (1, 1), stride=(1, 1)),  # transpose, conv could enhance fiting ability slightly
            Rearrange('b e (h) (w) -> b (h w) e'),
        )


    def forward(self, x: Tensor) -> Tensor:
        b, _, _, _ = x.shape
        x = self.shallownet(x)
        x = self.projection(x)
        return x


class MultiHeadAttention(nn.Module):
    def __init__(self, emb_size, num_heads, dropout):
        super().__init__()
        self.emb_size = emb_size
        self.num_heads = num_heads
        self.keys = nn.Linear(emb_size, emb_size)
        self.queries = nn.Linear(emb_size, emb_size)
        self.values = nn.Linear(emb_size, emb_size)
        self.att_drop = nn.Dropout(dropout)
        self.projection = nn.Linear(emb_size, emb_size)

    def forward(self, x: Tensor, mask: Tensor = None) -> Tensor:
        queries = rearrange(self.queries(x), "b n (h d) -> b h n d", h=self.num_heads)
        keys = rearrange(self.keys(x), "b n (h d) -> b h n d", h=self.num_heads)
        values = rearrange(self.values(x), "b n (h d) -> b h n d", h=self.num_heads)
        energy = torch.einsum('bhqd, bhkd -> bhqk', queries, keys)  
        if mask is not None:
            fill_value = torch.finfo(torch.float32).min
            energy.mask_fill(~mask, fill_value)

        scaling = self.emb_size ** (1 / 2)
        att = F.softmax(energy / scaling, dim=-1)
        att = self.att_drop(att)
        out = torch.einsum('bhal, bhlv -> bhav ', att, values)
        out = rearrange(out, "b h n d -> b n (h d)")
        out = self.projection(out)
        return out


class ResidualAdd(nn.Module):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def forward(self, x, **kwargs):
        res = x
        x = self.fn(x, **kwargs)
        x += res
        return x


class FeedForwardBlock(nn.Sequential):
    def __init__(self, emb_size, expansion, drop_p):
        super().__init__(
            nn.Linear(emb_size, expansion * emb_size),
            nn.GELU(),
            nn.Dropout(drop_p),
            nn.Linear(expansion * emb_size, emb_size),
        )


class GELU(nn.Module):
    def forward(self, input: Tensor) -> Tensor:
        return input*0.5*(1.0+torch.erf(input/math.sqrt(2.0)))


class TransformerEncoderBlock(nn.Sequential):
    def __init__(self,
                 emb_size,
                 num_heads=2,
                 drop_p=0.5,
                 forward_expansion=4,
                 forward_drop_p=0.5):
        super().__init__(
            ResidualAdd(nn.Sequential(
                nn.LayerNorm(emb_size),
                MultiHeadAttention(emb_size, num_heads, drop_p),
                nn.Dropout(drop_p)
            )),
            ResidualAdd(nn.Sequential(
                nn.LayerNorm(emb_size),
                FeedForwardBlock(
                    emb_size, expansion=forward_expansion, drop_p=forward_drop_p),
                nn.Dropout(drop_p)
            )
            ))


class TransformerEncoder(nn.Sequential):
    def __init__(self, depth, emb_size):
        super().__init__(*[TransformerEncoderBlock(emb_size) for _ in range(depth)])


class ClassificationHead(nn.Sequential):
    def __init__(self, emb_size, n_classes):
        super().__init__()
        
        # global average pooling
        self.clshead = nn.Sequential(
            Reduce('b n e -> b e', reduction='mean'),
            nn.LayerNorm(emb_size),
            nn.Linear(emb_size, n_classes)
        )
        self.fc = nn.Sequential(
            nn.Linear(3260, 256),
            nn.ELU(),
            nn.Dropout(0.5),
            nn.Linear(256, 32),
            nn.ELU(),
            nn.Dropout(0.3),
            nn.Linear(32, 4)
        )

    def forward(self, x):
        x = x.contiguous().view(x.size(0), -1)
        out = self.fc(x)
        return x, out


class Conformer(nn.Sequential):
    def __init__(self, emb_size=10, depth=6, n_classes=4, **kwargs):
        super().__init__(

            PatchEmbedding(emb_size),
            TransformerEncoder(depth, emb_size),
            ClassificationHead(emb_size, n_classes)
        )


class ExP():
    def __init__(self, nsub):
        super(ExP, self).__init__()
        self.batch_size = 72
        self.n_epochs = 100
        self.c_dim = 4
        self.lr = 0.0002
        self.b1 = 0.5
        self.b2 = 0.999
        self.dimension = (190, 50)
        self.nSub = nsub

        self.start_epoch = 0
        self.root = '/Users/siddharth/Documents/MATLAB/Examples/R2022b/images/BasicImageImportProcessingAndExportExample'

        self.log_write = open("/Users/siddharth/Desktop/MEG_Analysis/log_subject%d.txt" % self.nSub, "w")


        self.Tensor = torch.FloatTensor
        self.LongTensor = torch.LongTensor

        self.criterion_l1 = torch.nn.L1Loss()
        self.criterion_l2 = torch.nn.MSELoss()
        self.criterion_cls = torch.nn.CrossEntropyLoss()

        self.model = Conformer()
        self.model = nn.DataParallel(self.model, device_ids=[i for i in range(len(gpus))])
        self.model = self.model
        # summary(self.model, (1, 22, 1000))


    # Segmentation and Reconstruction (S&R) data augmentation
    # def interaug(self, timg, label):  
    #     aug_data = []
    #     aug_label = []
    #     for cls4aug in range(4):
    #         cls_idx = np.where(label == cls4aug + 1)
    #         tmp_data = timg[cls_idx]
    #         tmp_label = label[cls_idx]

    #         tmp_aug_data = np.zeros((int(self.batch_size / 4), 1, 22, 1000))
    #         for ri in range(int(self.batch_size / 4)):
    #             for rj in range(8):
    #                 rand_idx = np.random.randint(0, tmp_data.shape[0], 8)
    #                 tmp_aug_data[ri, :, :, rj * 125:(rj + 1) * 125] = tmp_data[rand_idx[rj], :, :,
    #                                                                   rj * 125:(rj + 1) * 125]

    #         aug_data.append(tmp_aug_data)
    #         aug_label.append(tmp_label[:int(self.batch_size / 4)])
    #     aug_data = np.concatenate(aug_data)
    #     aug_label = np.concatenate(aug_label)
    #     aug_shuffle = np.random.permutation(len(aug_data))
    #     aug_data = aug_data[aug_shuffle, :, :]
    #     aug_label = aug_label[aug_shuffle]

    #     aug_data = torch.from_numpy(aug_data).cuda()
    #     aug_data = aug_data.float()
    #     aug_label = torch.from_numpy(aug_label-1).cuda()
    #     aug_label = aug_label.long()
    #     return aug_data, aug_label
# # -----------------------

    def get_source_data(self):
        # Load training data
        train_total_data = scipy.io.loadmat(self.root + '/trial_mag5.mat')
        self.allLabel = train_total_data['original_data']
        self.train_data = train_total_data['trial_new2']
        self.train_data = np.transpose(self.train_data, (2, 1, 0))
        self.train_data = np.expand_dims(self.train_data, axis=1)
        self.allData = self.train_data
        target_mean = np.mean(self.allData)
        target_std = np.std(self.allData)
    # # Extract labels from training data
    #     self.Filter_train=self.total_data['Filt_MEG_Data']
    #     self.allLabel = self.Filter['trialinfo']  # Assuming 'labels' is a field in Filt_MEG_Data
        # shuffle_num = np.random.permutation(len(self.allData))
        # self.allData = self.allData[shuffle_num, :, :, :]
        # self.allLabel = self.allLabel[shuffle_num]

    # Load testing data
        test_total_data = scipy.io.loadmat(self.root + '/trial_mag6.mat')
        self.te_data = test_total_data['trial_new2']
        self.te_data = np.transpose(self.te_data, (2, 1, 0))
        self.te_data = np.expand_dims(self.te_data, axis=1)
        self.test_data = self.te_data[0:100]
        # self.Filter_test=self.te_data['Filt_MEG_Data']
        self.test_label = test_total_data['original_data']  # Assuming 'labels' is a field in Filt_MEG_Data

        self.testData = self.te_data
        self.testLabel = self.test_label

        # Load validation data
        self.val_data = self.te_data[100:200]
        # self.Filter_val=self.val_data['Filt_MEG_Data']
        self.val_label = test_total_data['original_data']  # Assuming 'labels' is a field in Filt_MEG_Data
        self.val_data = np.expand_dims(self.val_data, axis=1)
        self.valLabel = self.val_label

    # Standardize data
        self.allData = (self.allData - target_mean) / target_std
        self.testData = (self.testData - target_mean) / target_std
        self.val_data = (self.val_data - target_mean) / target_std

        # Data shape: (trial, conv channel, electrode channel, time samples)
        return self.allData, self.allLabel, self.testData, self.testLabel, self.val_data, self.valLabel



    def train(self):

        data=self.get_source_data()
        img, label, test_data, test_label = data[:4]
        # img = np.transpose(img)  # Shape: (7000, 1)
        # label = np.transpose(label)
        # test_data = np.transpose(test_data)
        # test_label = np.transpose(test_label)
        img = torch.from_numpy(img)
        img = img.permute(0, 2, 1, 3)
        # img = img.permute(0, 3, 1, 2)
        label = torch.from_numpy(label - 1)
        # img = img.unsqueeze(2)
        print("Sizes before creating TensorDataset:")
        print("img size:", img.size())
        print("label size:", label.size())
        dataset = torch.utils.data.TensorDataset(img, label)
        self.dataloader = torch.utils.data.DataLoader(dataset=dataset, batch_size=self.batch_size, shuffle=True)

        test_data = torch.from_numpy(test_data)
        test_data = test_data.permute(0, 2, 1, 3)
        test_label = torch.from_numpy(test_label -   1)
        test_dataset = torch.utils.data.TensorDataset(test_data, test_label)
        self.test_dataloader = torch.utils.data.DataLoader(dataset=test_dataset, batch_size=self.batch_size, shuffle=True)

        # Optimizers
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=self.lr, betas=(self.b1, self.b2))

        test_data = Variable(test_data.type(self.Tensor))
        test_label = Variable(test_label.squeeze().type(self.LongTensor))

        bestAcc = 0
        averAcc = 0
        num = 0
        Y_true = 0
        Y_pred = 0
        # Train the cnn model
        total_step = len(self.dataloader)
        curr_lr = self.lr
        print(img.shape,label.shape,test_label.shape,test_data.shape)
        for e in range(self.n_epochs):
            # in_epoch = time.time()
            self.model.train()
            for i, (img, label) in enumerate(self.dataloader):

                img = Variable(img.type(self.Tensor))
                label = Variable(label.squeeze().type(self.LongTensor))

                # data augmentation
                # aug_data, aug_label = self.interaug(self.allData, self.allLabel)
                # img = torch.cat((img, aug_data))
                # label = torch.cat((label, aug_label))


                tok, outputs = self.model(img)

                loss = self.criterion_cls(outputs, label) 

                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()


            # out_epoch = time.time()


            # test process
            if (e + 1) % 1 == 0:
                self.model.eval()
                Tok, Cls = self.model(test_data)


                loss_test = self.criterion_cls(Cls, test_label)
                y_pred = torch.max(Cls, 1)[1]
                acc = float((y_pred == test_label).cpu().numpy().astype(int).sum()) / float(test_label.size(0))
                train_pred = torch.max(outputs, 1)[1]
                train_acc = float((train_pred == label).cpu().numpy().astype(int).sum()) / float(label.size(0))

                print('Epoch:', e,
                    '  Train loss: %.6f' % loss.detach().cpu().numpy(),
                    '  Test loss: %.6f' % loss_test.detach().cpu().numpy(),
                    '  Train accuracy %.6f' % train_acc,
                    '  Test accuracy is %.6f' % acc)

                self.log_write.write(str(e) + "    " + str(acc) + "\n")
                num = num + 1
                averAcc = averAcc + acc
                if acc > bestAcc:
                    bestAcc = acc
                    Y_true = test_label
                    Y_pred = y_pred


        torch.save(self.model.module.state_dict(), 'model.pth')
        averAcc = averAcc / num
        print('The average accuracy is:', averAcc)
        print('The best accuracy is:', bestAcc)
        self.log_write.write('The average accuracy is: ' + str(averAcc) + "\n")
        self.log_write.write('The best accuracy is: ' + str(bestAcc) + "\n")

        return bestAcc, averAcc, Y_true, Y_pred
        # writer.close()


def main():
    best = 0
    aver = 0
    result_write = open("/Users/siddharth/Desktop/MEG_Analysis/sub_result.txt", "w")

    for i in range(9):
        starttime = datetime.datetime.now()


        seed_n = np.random.randint(2021)
        print('seed is ' + str(seed_n))
        # random.seed(seed_n)
        # np.random.seed(seed_n)
        # torch.manual_seed(seed_n)
        # torch.manual_seed(seed_n)
        # torch.manual_seed_all(seed_n)


        print('Subject %d' % (i+1))
        exp = ExP(i + 1)

        bestAcc, averAcc, Y_true, Y_pred = exp.train()
        print('THE BEST ACCURACY IS ' + str(bestAcc))
        result_write.write('Subject ' + str(i + 1) + ' : ' + 'Seed is: ' + str(seed_n) + "\n")
        result_write.write('Subject ' + str(i + 1) + ' : ' + 'The best accuracy is: ' + str(bestAcc) + "\n")
        result_write.write('Subject ' + str(i + 1) + ' : ' + 'The average accuracy is: ' + str(averAcc) + "\n")

        endtime = datetime.datetime.now()
        print('subject %d duration: '%(i+1) + str(endtime - starttime))
        best = best + bestAcc
        aver = aver + averAcc
        if i == 0:
            yt = Y_true
            yp = Y_pred
        else:
            yt = torch.cat((yt, Y_true))
            yp = torch.cat((yp, Y_pred))


    best = best / 9
    aver = aver / 9

    result_write.write('**The average Best accuracy is: ' + str(best) + "\n")
    result_write.write('The average Aver accuracy is: ' + str(aver) + "\n")
    result_write.close()


if __name__ == "__main__":
    print(time.asctime(time.localtime(time.time())))
    main()
    print(time.asctime(time.localtime(time.time())))
