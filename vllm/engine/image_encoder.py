import math
import os
import re
from typing import List

import PIL.Image
import numpy as np
import torch
import torchvision.transforms as transforms
from PIL import Image
from peft import LoraConfig, TaskType, get_peft_model
from torchvision.transforms.functional import InterpolationMode
from transformers import CLIPVisionModel


class Projector(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.vision_proj = build_vision_projector()

    def forward(self, x):
        return self.vision_proj(x)


class ImageEncoder:
    def __init__(self, weights_path=None, device='cpu'):
        self.plora_glb_GN = torch.zeros([1, 1, 4096], device=device)
        self.plora_sub_GN = torch.zeros([1, 1, 1, 4096], device=device)

        states = torch.load(weights_path, map_location='cpu')

        if any('lora_A' in key for key in states['vit'].keys()):
            lora_r = 512
            peft_config = LoraConfig(
                inference_mode=True,
                r=lora_r,
                lora_alpha=lora_r // 2,
                lora_dropout=0.05,
                target_modules=[
                    'attention.q_proj.linear',
                    'attention.k_proj.linear',
                    'attention.v_proj.linear',
                    'attention.wo.linear',
                    'feed_forward.w1.linear',
                    'feed_forward.w2.linear',
                    'feed_forward.w3.linear',
                    'vision_proj.0', 'vision_proj.2',
                    'self_attn.q_proj', 'self_attn.k_proj', 'self_attn.v_proj',
                    'out_proj', 'mlp.fc1', 'mlp.fc2'
                ],
                modules_to_save=[
                    'tree_avgpool_scaler',
                    'input_layernorm', 'post_attention_layernorm'
                ]
            )
        else:
            peft_config = None

        self.vit = build_vision_tower().to(device)
        self.vision_proj = Projector().to(device)
        if peft_config is not None:
            self.vit = get_peft_model(self.vit, peft_config)
            self.vision_proj = get_peft_model(self.vision_proj, peft_config)
        self.tok_embeddings = torch.nn.Embedding(92544, 4096, device=device)
        self.device = device

        self.vit.base_model.model.load_state_dict(states['vit'])
        self.vision_proj.base_model.model.vision_proj.load_state_dict(states['vision_proj'])
        self.plora_sub_GN.copy_(states['plora_sub_GN'])
        self.plora_glb_GN.copy_(states['plora_glb_GN'])
        self.tok_embeddings.load_state_dict(states['tok_embeddings'])

        self.vis_processor = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize((0.48145466, 0.4578275, 0.40821073),
                                 (0.26862954, 0.26130258, 0.27577711)),
        ])

    def embed_tokens(self, tokens: List[int]):
        return self.tok_embeddings(torch.tensor(tokens, device=self.device))

    def encode_one_image(self, image: PIL.Image, hd_num=25):  # 55 for 4KHD
        """Encode one image into a tensor."""
        if image is None:
            return None
        image = HD_transform(image, hd_num=hd_num)
        image = self.vis_processor(image).unsqueeze(0).to(self.device)

        img_embeds = self.img2emb(image)
        return img_embeds.squeeze(0)

    def img2emb(self, image):
        img_embeds, img_split = self.vit([image], self.plora_glb_GN, self.plora_sub_GN)
        if len(img_split) > 1:
            print('Batch Size >1 is not supported.')
            assert 0

        print(img_embeds.shape)
        img_embeds = self.vision_proj(img_embeds)
        return img_embeds


def padding_336(b):
    width, height = b.size
    tar = int(np.ceil(height / 336) * 336)
    top_padding = int((tar - height) / 2)
    bottom_padding = tar - height - top_padding
    left_padding = 0
    right_padding = 0
    b = transforms.functional.pad(b, [left_padding, top_padding, right_padding, bottom_padding], fill=[255, 255, 255])
    return b


def HD_transform(img, hd_num=16):
    width, height = img.size
    trans = False
    if width < height:
        img = img.transpose(Image.TRANSPOSE)
        trans = True
        width, height = img.size
    ratio = (width / height)
    scale = 1
    while scale * np.ceil(scale / ratio) <= hd_num:
        scale += 1
    scale -= 1
    new_w = int(scale * 336)
    new_h = int(new_w / ratio)

    img = transforms.functional.resize(img, [new_h, new_w], )
    img = padding_336(img)
    if trans:
        img = img.transpose(Image.TRANSPOSE)

    return img


def build_vision_tower():
    vision_tower = 'openai/clip-vit-large-patch14-336'
    return CLIPVisionTower(vision_tower)


def build_vision_projector():
    projector_type = 'mlp2x_gelu'
    mm_hidden_size = 4096
    mid_hidden_size = 4096
    hidden_size = 4096

    mlp_gelu_match = re.match(r'^mlp(\d+)x_gelu$', projector_type)
    if mlp_gelu_match:
        mlp_depth = int(mlp_gelu_match.group(1))
        modules = [torch.nn.Linear(mm_hidden_size, mid_hidden_size)]
        for _ in range(1, mlp_depth):
            modules.append(torch.nn.GELU())
            modules.append(torch.nn.Linear(mid_hidden_size, mid_hidden_size))

        return torch.nn.Sequential(*modules)

    if projector_type == 'identity':
        return IdentityMap()

    raise ValueError(f'Unknown projector type: {projector_type}')


class IdentityMap(torch.nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, x, *args, **kwargs):
        return x

    @property
    def config(self):
        return {"mm_projector_type": 'identity'}


class CLIPVisionTower(torch.nn.Module):
    def __init__(self, vision_tower):
        super().__init__()

        self.is_loaded = False

        self.vision_tower_name = vision_tower
        self.select_layer = -1
        self.select_feature = 'patch'
        self.load_model()

    def load_model(self):
        self.vision_tower = CLIPVisionModel.from_pretrained(self.vision_tower_name)
        self.vision_tower.requires_grad_(False)

        self.is_loaded = True

    def resize_pos(self):
        print('Dummy Resized')

    def feature_select(self, image_forward_outs):
        image_features = image_forward_outs.hidden_states[self.select_layer]
        if self.select_feature == 'patch':
            image_features = image_features[:, 1:]
        elif self.select_feature == 'cls_patch':
            image_features = image_features
        else:
            raise ValueError(f'Unexpected select feature: {self.select_feature}')
        return image_features

    def forward(self, images, glb_GN, sub_GN):
        if not self.is_loaded:
            self.load_model()
        assert type(images) is list
        shapes = []
        input_imgs = []
        for img in images:
            _, C, H, W = img.shape
            shapes.append([H // 336, W // 336])
            sub_img = img.reshape(1, 3, H // 336, 336, W // 336, 336) \
                .permute(0, 2, 4, 1, 3, 5).reshape(-1, 3, 336, 336).contiguous()
            glb_img = torch.nn.functional.interpolate(img.float(), size=(336, 336), mode='bicubic').to(sub_img.dtype)
            input_imgs.append(glb_img)
            input_imgs.append(sub_img)
        input_imgs = torch.cat(input_imgs, dim=0)

        image_forward_outs = self.vision_tower(input_imgs.to(device=self.device, dtype=self.dtype),
                                               output_hidden_states=True)
        image_features = self.feature_select(image_forward_outs).to(input_imgs.dtype)  ### B*?, N, C
        _, N, C = image_features.shape
        H = int(math.sqrt(N))
        assert N == 24 ** 2

        output_imgs = []
        output_len = []
        for [h, w] in shapes:
            B_ = h * w
            glb_img = image_features[:1]  ### 1, N, C
            glb_img = glb_img.reshape(1, H, H, C) \
                .reshape(1, H // 2, 2, H // 2, 2, C).contiguous() \
                .permute(0, 1, 3, 2, 4, 5).reshape(1, H // 2, H // 2, 4 * C).contiguous()
            temp_glb_GN = sub_GN.repeat(1, H // 2, 1, 1)
            glb_img = torch.cat([glb_img, temp_glb_GN], dim=2).reshape(1, -1, 4 * C)

            sub_img = image_features[1:1 + B_]  ### ?, N, C
            sub_img = sub_img.reshape(B_, H, H, C) \
                .reshape(B_, H // 2, 2, H // 2, 2, C).contiguous() \
                .permute(0, 1, 3, 2, 4, 5) \
                .reshape(B_, -1, 4 * C).contiguous()
            sub_img = sub_img.reshape(1, h, w, 12, 12, -1).permute(0, 1, 3, 2, 4, 5).reshape(1, h * 12, w * 12, 4 * C)
            temp_sub_GN = sub_GN.repeat(1, h * 12, 1, 1)
            sub_img = torch.cat([sub_img, temp_sub_GN], dim=2).reshape(1, -1, 4 * C)

            output_imgs.append(torch.cat([glb_img, glb_GN, sub_img], dim=1))
            temp_len = int((h * w + 1) * 144 + 1 + (h + 1) * 12)
            assert temp_len == output_imgs[-1].shape[1]
            output_len.append(temp_len)

            image_features = image_features[1 + h * w:]

        output_imgs = torch.cat(output_imgs, dim=1)

        return output_imgs, output_len

    @property
    def dummy_feature(self):
        return torch.zeros(1, self.hidden_size, device=self.device, dtype=self.dtype)

    @property
    def dtype(self):
        return self.vision_tower.dtype

    @property
    def device(self):
        return self.vision_tower.device

    @property
    def config(self):
        if self.is_loaded:
            return self.vision_tower.config
        else:
            return self.cfg_only

    @property
    def hidden_size(self):
        return self.config.hidden_size

    @property
    def num_patches(self):
        return (self.config.image_size // self.config.patch_size) ** 2
