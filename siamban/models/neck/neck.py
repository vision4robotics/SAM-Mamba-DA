# Copyright (c) SenseTime. All Rights Reserved.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import torch.nn as nn
from siamban.models.neck.trans import Transformer
from siamban.models.vmamba.vmamba import VMambaNeck, VMambaNeckV2
import torch

class AdjustLayer(nn.Module):
    def __init__(self, in_channels, out_channels):
        super(AdjustLayer, self).__init__()
        self.downsample = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size=1, bias=False),
            nn.BatchNorm2d(out_channels),
            )

    def forward(self, x):
        x = self.downsample(x)
        if x.size(3) < 20:
            l = 4
            r = l + 7
            x = x[:, :, l:r, l:r]
        return x


class AdjustAllLayer(nn.Module):
    def __init__(self, in_channels, out_channels):
        super(AdjustAllLayer, self).__init__()
        self.num = len(out_channels)
        if self.num == 1:
            self.downsample = AdjustLayer(in_channels[0], out_channels[0])
        else:
            for i in range(self.num):
                self.add_module('downsample'+str(i+2),
                                AdjustLayer(in_channels[i], out_channels[i]))

    def forward(self, features):
        if self.num == 1:
            return self.downsample(features)
        else:
            out = []
            for i in range(self.num):
                adj_layer = getattr(self, 'downsample'+str(i+2))
                out.append(adj_layer(features[i]))
            return out


class Adjust_Transformer(nn.Module):
    def __init__(self, channels=256):
        super(Adjust_Transformer, self).__init__()

        self.row_embed = nn.Embedding(50, channels//2)
        self.col_embed = nn.Embedding(50, channels//2)
        self.reset_parameters()

        self.transformer = Transformer(channels, nhead = 8, num_encoder_layers = 1, num_decoder_layers = 0)

    def reset_parameters(self):
        nn.init.uniform_(self.row_embed.weight)
        nn.init.uniform_(self.col_embed.weight)

    def forward(self, x_f):
        # adjust search features
        # x_f [batchsize, 256, 31, 31] / [batchsize, 256, 7, 7]
        h, w = x_f.shape[-2:]
        i = torch.arange(w).cuda()
        j = torch.arange(h).cuda()
        x_emb = self.col_embed(i)
        y_emb = self.row_embed(j)
        pos = torch.cat([
            x_emb.unsqueeze(0).repeat(h, 1, 1),
            y_emb.unsqueeze(1).repeat(1, w, 1),
            ], dim= -1).permute(2, 0, 1).unsqueeze(0).repeat(x_f.shape[0], 1, 1, 1)
        b, c, w, h = x_f.size()
        x_f = self.transformer((pos+x_f).view(b, c, -1).permute(2, 0, 1),\
                                (pos+x_f).view(b, c, -1).permute(2, 0, 1),\
                                    (pos+x_f).view(b, c, -1).permute(2, 0, 1))
        x_f = x_f.permute(1, 2, 0).view(b, c, w, h)
        return x_f


class Adjust_VMamba(nn.Module):
    def __init__(self, channels=256, depth=1):
        super(Adjust_VMamba, self).__init__()
        self.mamba = VMambaNeck(depths=[1]*depth, dims=[channels]*depth)

    def forward(self, x):
        # adjust search features
        # x_f [batchsize, 256, 31, 31] / [batchsize, 256, 7, 7]
        b, c, h, w = x.shape
        x = x.permute(0, 2, 3, 1)  # b, h, w, c
        x = self.mamba(x)          # b, h, w, c
        x = x.permute(0, 3, 1, 2)  # b, c, h, w
        return x


class Adjust_VMambaV2(nn.Module):
    def __init__(self, channels=256, depth=1):
        super(Adjust_VMambaV2, self).__init__()
        self.mamba = VMambaNeckV2(depths=[1]*depth, dims=[channels]*depth)
        self.channel = channels

    def forward(self, x):
        # adjust search features
        # x_f [batchsize, 256, 31, 31] / [batchsize, 256, 7, 7]
        b, c, h, w = x.shape
        x = x.permute(0, 2, 3, 1)  # b, h, w, c
        x = self.mamba(x)          # b, h, w, c
        x = x.permute(0, 3, 1, 2)  # b, c, h, w
        return x

if __name__ == '__main__':
    from thop import profile
    from thop import clever_format
    import time
    import torch.cuda.profiler as profiler

    # extracted search features
    data = torch.randn(1, 256, 31, 31).cuda()

    # calculate flops for vmamba bridger and transformer bridger
    # module = Adjust_VMamba(channels=256, depth=1).cuda()
    module = Adjust_Transformer().cuda()
    repeatnum = 10000
    profiler.start()
    with torch.no_grad():
        for _ in range(repeatnum):
            _ = module(data)
    profiler.stop()
    events = profiler.profile()
    flops = 0
    for event in events:
        if 'FLOPs' in event.name:
            flops += event.count
    print(f"estimated flops: {flops/repeatnum}")

    # calculate the throughput of vmmba bridger or transformer bridger
    timelist = []
    with torch.no_grad():
        for _ in range(repeatnum):
            start_time = time.time()
            _ = module(data)
            timelist.append(time.time()-start_time)
    throughput = repeatnum / sum(timelist)
    print(f"throughput:{throughput:.2f}fps ")


