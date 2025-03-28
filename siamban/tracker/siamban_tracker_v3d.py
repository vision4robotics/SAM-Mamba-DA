from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import numpy as np
print(np.__version__)

from siamban.core.config import cfg
from siamban.tracker.base_tracker import SiameseTracker
from siamban.utils.bbox import corner2center
from siamban.tracker.DCE.lowlight_enhancement import *
from siamban.tracker.DCE.dce_model import *
from .gradcam import GradCAM
from .utils import visualize_cam, visualize_cam_3d
from torchvision.utils import make_grid, save_image
import os
import torch
import torch.nn.functional as F


class SiamBANTracker(SiameseTracker):
    def __init__(self, model):
        super(SiamBANTracker, self).__init__()
        self.score_size = (cfg.TRACK.INSTANCE_SIZE - cfg.TRACK.EXEMPLAR_SIZE) // \
            cfg.POINT.STRIDE + 1 + cfg.TRACK.BASE_SIZE
        hanning = np.hanning(self.score_size)
        window = np.outer(hanning, hanning)
        self.cls_out_channels = cfg.BAN.KWARGS.cls_out_channels
        self.window = window.flatten()
        self.points = self.generate_points(cfg.POINT.STRIDE, self.score_size)
        self.model = model
        self.model.eval()
        self.idx = 0
        self.videoname = ''

        scale_factor = 12
        self.enhancer = enhance_net_nopool(scale_factor).cuda().eval()
        self.enhancer.load_state_dict(torch.load('/mnt/sdc/V4R/WYH/SAM-Mamba-DA/clean/BAN/siamban/tracker/DCE/Epoch99.pth'))

    def generate_points(self, stride, size):
        ori = - (size // 2) * stride
        x, y = np.meshgrid([ori + stride * dx for dx in np.arange(0, size)],
                           [ori + stride * dy for dy in np.arange(0, size)])
        points = np.zeros((size * size, 2), dtype=np.float32)
        points[:, 0], points[:, 1] = x.astype(np.float32).flatten(), y.astype(np.float32).flatten()

        return points

    def _convert_bbox(self, delta, point):
        delta = delta.permute(1, 2, 3, 0).contiguous().view(4, -1)
        delta = delta.detach().cpu().numpy()

        delta[0, :] = point[:, 0] - delta[0, :]
        delta[1, :] = point[:, 1] - delta[1, :]
        delta[2, :] = point[:, 0] + delta[2, :]
        delta[3, :] = point[:, 1] + delta[3, :]
        delta[0, :], delta[1, :], delta[2, :], delta[3, :] = corner2center(delta)
        return delta

    def _convert_score(self, score):
        if self.cls_out_channels == 1:
            score = score.permute(1, 2, 3, 0).contiguous().view(-1)
            score = score.sigmoid().detach().cpu().numpy()
        else:
            score = score.permute(1, 2, 3, 0).contiguous().view(self.cls_out_channels, -1).permute(1, 0)
            score = score.softmax(1).detach()[:, 1].cpu().numpy()
        return score

    def _bbox_clip(self, cx, cy, width, height, boundary):
        cx = max(0, min(cx, boundary[1]))
        cy = max(0, min(cy, boundary[0]))
        width = max(10, min(width, boundary[1]))
        height = max(10, min(height, boundary[0]))
        return cx, cy, width, height

    def init(self, img, bbox):
        """
        args:
            img(np.ndarray): BGR image
            bbox: (x, y, w, h) bbox
        """
        self.center_pos = np.array([bbox[0]+(bbox[2]-1)/2,
                                    bbox[1]+(bbox[3]-1)/2])
        self.size = np.array([bbox[2], bbox[3]])

        # calculate z crop size
        w_z = self.size[0] + cfg.TRACK.CONTEXT_AMOUNT * np.sum(self.size)
        h_z = self.size[1] + cfg.TRACK.CONTEXT_AMOUNT * np.sum(self.size)
        s_z = round(np.sqrt(w_z * h_z))

        # calculate channle average
        self.channel_average = np.mean(img, axis=(0, 1))

        # get crop
        z_crop = self.get_subwindow(img, self.center_pos,
                                    cfg.TRACK.EXEMPLAR_SIZE,
                                    s_z, self.channel_average)
        self.model.template(z_crop)

        self.apn_model_dict = dict(type='apn', arch=self.model, layer_name='features_11', input_size=(255, 255))
        self.alexnet_gradcam = GradCAM(self.apn_model_dict, True)

    def track(self, img, idx, video_name, model_name, dataset_name):
        """
        args:
            img(np.ndarray): BGR image
        return:
            bbox(list):[x, y, width, height]
        """
        w_z = self.size[0] + cfg.TRACK.CONTEXT_AMOUNT * np.sum(self.size)
        h_z = self.size[1] + cfg.TRACK.CONTEXT_AMOUNT * np.sum(self.size)
        s_z = np.sqrt(w_z * h_z)
        scale_z = cfg.TRACK.EXEMPLAR_SIZE / s_z
        s_x = s_z * (cfg.TRACK.INSTANCE_SIZE / cfg.TRACK.EXEMPLAR_SIZE)
        x_crop = self.get_subwindow(img, self.center_pos,
                                    cfg.TRACK.INSTANCE_SIZE,
                                    round(s_x), self.channel_average)
        # change the videoname and idx
        if self.videoname != video_name:
            self.videoname = video_name
            self.idx = 0

        # # save crop
        # images = []
        # images.append(torch.stack([x_crop.div(255).squeeze().cpu()],0))
        # images = make_grid(torch.cat(images, 0), nrow=1)
        # output_dir = f'heatmap3d/{dataset_name}/{video_name}/{model_name}/crop'
        # os.makedirs(output_dir, exist_ok=True)
        # output_name = f'{self.idx}.jpeg'
        # output_path = os.path.join(output_dir, output_name)
        # save_image(images, output_path)

        # if (video_name == "N04006" and 260 <= idx <= 265) \
        #     or (video_name == "N02018" and 180 <= idx <= 185) \
        #     or (video_name == "N02011" and 143 <= idx <= 146) \
        #     or (video_name == "N04008" and 281 <= idx <= 295) \
        #     or (video_name == "N04003" and 103 <= idx <= 110) \
        #     or (video_name == 'car15' and 235 <= idx <= 245 ) \
        #     or (video_name == 'girl1' and 30 <= idx <= 40 ) \
        #     or (video_name == 'motorbike2' and 100 <= idx <= 110 ) \
        #     or (video_name == 'person15' and 105 <= idx <= 115 ):
        if (video_name == "N04008" and 292 == idx) \
            or (video_name == "N04003" and 107 == idx) \
            or (video_name == 'car15' and 241 == idx) \
            or (video_name == 'girl1' and 35 == idx) \
            or (video_name == 'person15' and 111 == idx):
            # save heatmap
            images = []
            res=[]
            mask,_=self.alexnet_gradcam(x_crop)
            output_dir = f'heatmap3d/{dataset_name}/{video_name}/{model_name}/heatmap'
            os.makedirs(output_dir, exist_ok=True)
            output_name = f'{self.idx}.jpeg'
            output_path = os.path.join(output_dir, output_name)
            visualize_cam_3d(mask, x_crop, output_path)

            # enhance full image
            output_dir = f'heatmap3d/{dataset_name}/{video_name}/{model_name}/heatmap-enhance-full-image'
            os.makedirs(output_dir, exist_ok=True)
            output_path = os.path.join(output_dir, output_name)
            enhanced_full_image = lowlight(self.enhancer, img).squeeze(0).detach().cpu().permute(1,2,0)
            enhanced_full_image = np.array(enhanced_full_image)
            print('img', type(img), img.shape)
            print('enhanced_full_image', type(enhanced_full_image), enhanced_full_image.shape)
            enhanced_x_crop = self.get_subwindow(enhanced_full_image, self.center_pos,
                                                 cfg.TRACK.INSTANCE_SIZE,
                                                 round(s_x), self.channel_average)
            visualize_cam_3d(mask, enhanced_x_crop, output_path)

            # save enhanced crop
            images = []
            images.append(torch.stack([enhanced_x_crop.squeeze().cpu()],0))
            images = make_grid(torch.cat(images, 0), nrow=1)
            output_dir = f'heatmap3d/{dataset_name}/{video_name}/{model_name}/crop-enhance-full-image'
            os.makedirs(output_dir, exist_ok=True)
            output_name = f'{self.idx}.jpeg'
            output_path = os.path.join(output_dir, output_name)
            save_image(images, output_path)

            # enhance cropped image
            output_dir = f'heatmap3d/{dataset_name}/{video_name}/{model_name}/heatmap-enhance-crop'
            os.makedirs(output_dir, exist_ok=True)
            output_path = os.path.join(output_dir, output_name)
            print('img', type(img), img.shape)
            print('x_crop', type(x_crop), x_crop.shape)
            enhanced_x_crop = np.array(x_crop.squeeze(0).cpu().permute(1,2,0))
            print('x_crop', type(enhanced_x_crop), enhanced_x_crop.shape)
            enhanced_x_crop = lowlight(self.enhancer, enhanced_x_crop).detach().cpu()
            enhanced_x_crop = F.interpolate(enhanced_x_crop, size=(255, 255), mode='bilinear', align_corners=False).squeeze(0)
            print('enhanced_x_crop', type(enhanced_x_crop), enhanced_x_crop.shape)
            visualize_cam_3d(mask, enhanced_x_crop, output_path)

            # save enhanced crop
            images = []
            images.append(torch.stack([enhanced_x_crop.squeeze().cpu()],0))
            images = make_grid(torch.cat(images, 0), nrow=1)
            output_dir = f'heatmap3d/{dataset_name}/{video_name}/{model_name}/crop-enhance-crop'
            os.makedirs(output_dir, exist_ok=True)
            output_name = f'{self.idx}.jpeg'
            output_path = os.path.join(output_dir, output_name)
            save_image(images, output_path)

        # idx += 1
        self.idx += 1

        outputs = self.model.track(x_crop)

        score = self._convert_score(outputs['cls'])
        pred_bbox = self._convert_bbox(outputs['loc'], self.points)

        def change(r):
            return np.maximum(r, 1. / r)

        def sz(w, h):
            pad = (w + h) * 0.5
            return np.sqrt((w + pad) * (h + pad))

        # scale penalty
        s_c = change(sz(pred_bbox[2, :], pred_bbox[3, :]) /
                     (sz(self.size[0]*scale_z, self.size[1]*scale_z)))

        # aspect ratio penalty
        r_c = change((self.size[0]/self.size[1]) /
                     (pred_bbox[2, :]/pred_bbox[3, :]))
        penalty = np.exp(-(r_c * s_c - 1) * cfg.TRACK.PENALTY_K)
        pscore = penalty * score

        # window penalty
        pscore = pscore * (1 - cfg.TRACK.WINDOW_INFLUENCE) + \
            self.window * cfg.TRACK.WINDOW_INFLUENCE
        best_idx = np.argmax(pscore)
        bbox = pred_bbox[:, best_idx] / scale_z
        lr = penalty[best_idx] * score[best_idx] * cfg.TRACK.LR

        cx = bbox[0] + self.center_pos[0]
        cy = bbox[1] + self.center_pos[1]

        # smooth bbox
        width = self.size[0] * (1 - lr) + bbox[2] * lr
        height = self.size[1] * (1 - lr) + bbox[3] * lr

        # clip boundary
        cx, cy, width, height = self._bbox_clip(cx, cy, width,
                                                height, img.shape[:2])

        # udpate state
        self.center_pos = np.array([cx, cy])
        self.size = np.array([width, height])

        bbox = [cx - width / 2,
                cy - height / 2,
                width,
                height]
        best_score = score[best_idx]
        return {
                'bbox': bbox,
                'best_score': best_score
               }
