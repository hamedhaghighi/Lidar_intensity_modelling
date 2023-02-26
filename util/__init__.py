"""This package includes a miscellaneous collection of useful helper functions."""
import gc
import os.path as osp

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from tqdm import tqdm

# from util.geometry import estimate_surface_normal


def init_weights(cfg):
    init_type = cfg.init.type
    gain = cfg.init.gain
    nonlinearity = cfg.relu_type

    def init_func(m):
        classname = m.__class__.__name__
        if classname.find("BatchNorm2d") != -1:
            if hasattr(m, "weight") and m.weight is not None:
                nn.init.normal_(m.weight, 1.0, gain)
            if hasattr(m, "bias") and m.bias is not None:
                nn.init.constant_(m.bias, 0.0)
        elif hasattr(m, "weight") and (
            classname.find("Conv") != -1 or classname.find("Linear") != -1
        ):
            if init_type == "normal":
                nn.init.normal_(m.weight, 0.0, gain)
            elif init_type == "xavier":
                nn.init.xavier_normal_(m.weight, gain=gain)
            elif init_type == "xavier_uniform":
                nn.init.xavier_uniform_(m.weight, gain=gain)
            elif init_type == "kaiming":
                if nonlinearity == "relu":
                    nn.init.kaiming_normal_(m.weight, 0, "fan_in", "relu")
                elif nonlinearity == "leaky_relu":
                    nn.init.kaiming_normal_(m.weight, 0.2, "fan_in", "learky_relu")
                else:
                    raise NotImplementedError(f"Unknown nonlinearity: {nonlinearity}")
            elif init_type == "orthogonal":
                nn.init.orthogonal_(m.weight, gain=gain)
            elif init_type == "none":  # uses pytorch's default init method
                m.reset_parameters()
            else:
                raise NotImplementedError(f"Unknown initialization: {init_type}")
            if hasattr(m, "bias") and m.bias is not None:
                nn.init.constant_(m.bias, 0.0)

    return init_func


def set_requires_grad(net, requires_grad: bool = True):
    for param in net.parameters():
        param.requires_grad = requires_grad


def zero_grad(optim):
    for group in optim.param_groups:
        for p in group["params"]:
            p.grad = None


def sigmoid_to_tanh(x: torch.Tensor):
    """[0,1] -> [-1,+1]"""
    out = x * 2.0 - 1.0
    return out


def tanh_to_sigmoid(x: torch.Tensor):
    """[-1,+1] -> [0,1]"""
    out = (x + 1.0) / 2.0
    return out


def get_device(cuda: bool):
    cuda = cuda and torch.cuda.is_available()
    device = torch.device("cuda" if cuda else "cpu")
    if cuda:
        for i in range(torch.cuda.device_count()):
            print("device {}: {}".format(i, torch.cuda.get_device_name(i)))
    else:
        print("device: CPU")
    return device


def noise(tensor: torch.Tensor, std: float = 0.1):
    noise = tensor.clone().normal_(0, std)
    return tensor + noise


def print_gc():
    # https://discuss.pytorch.org/t/how-to-debug-causes-of-gpu-memory-leaks/6741
    for obj in gc.get_objects():
        try:
            if torch.is_tensor(obj) or (
                hasattr(obj, "data") and torch.is_tensor(obj.data)
            ):
                print(type(obj), obj.size())
        except:
            pass


def cycle(iterable):
    while True:
        for i in iterable:
            yield i




def postprocess(synth, lidar, tol=1e-8, normal_mode="closest", data_maps=None):

    out = {}
    for key, value in synth.items():
        if 'inv' in key:
            out[key] = tanh_to_sigmoid(value).clamp_(0, 1)
            out["points_" + key] = lidar.inv_to_xyz(value, tol)
        elif "reflectance" in key:
            out[key] = tanh_to_sigmoid(value).clamp_(0, 1)
        elif 'label' in key:
            out[key] = _map(_map(value,data_maps.inv_learning_map), data_maps.color_map)[..., ::-1]
        else:
            out[key] = value
    return out


def save_videos(frames, filename, fps=30.0):
    N = len(frames)
    H, W, C = frames[0].shape
    codec = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(filename + ".mp4", codec, fps, (W, H))
    for frame in tqdm(frames, desc="Writing..."):
        writer.write(frame[..., ::-1])
    writer.release()
    cv2.destroyAllWindows()
    print("Saved:", filename)


def colorize(tensor, cmap="turbo"):
    if tensor.ndim == 4:
        B, C, H, W = tensor.shape
        assert C == 1, f"expected (B,1,H,W) tensor, but got {tensor.shape}"
        tensor = tensor.squeeze(1)
    assert tensor.ndim == 3, f"got {tensor.ndim}!=3"

    device = tensor.device

    colors = eval(f"cm.{cmap}")(np.linspace(0, 1, 256))[:, :3]
    color_map = torch.tensor(colors, device=device, dtype=tensor.dtype)  # (256,3)

    tensor = tensor.clamp_(0, 1)
    tensor = tensor * 255.0
    index = torch.round(tensor).long()

    return F.embedding(index, color_map).permute(0, 3, 1, 2)


def flatten(tensor_BCHW):
    return tensor_BCHW.flatten(2).permute(0, 2, 1).contiguous()


def xyz_to_normal(xyz, mode="closest"):
    normals = -estimate_surface_normal(xyz, mode=mode)
    normals[normals != normals] = 0.0
    normals = tanh_to_sigmoid(normals).clamp_(0.0, 1.0)
    return normals


class SphericalOptimizer(torch.optim.Adam):
    def __init__(self, params, **kwargs):
        super().__init__(params, **kwargs)
        self.params = params

    @torch.no_grad()
    def step(self, closure=None):
        loss = super().step(closure)
        for param in self.params:
            param.data.div_(param.pow(2).mean(dim=1, keepdim=True).add(1e-9).sqrt())
        return loss


def masked_loss(img_ref, img_gen, mask, distance="l1"):
    if distance == "l1":
        loss = F.l1_loss(img_ref, img_gen, reduction="none")
    elif distance == "l2":
        loss = F.mse_loss(img_ref, img_gen, reduction="none")
    else:
        raise NotImplementedError
    loss = (loss * mask).sum(dim=(1, 2, 3))
    loss = loss / mask.sum(dim=(1, 2, 3))
    return loss


def _map(label, mapdict):
    # put label from original values to xentropy
    # or vice-versa, depending on dictionary values
    # make learning map a lookup table
    maxkey = 0
    for key, data in mapdict.items():
        if isinstance(data, list):
            nel = len(data)
        else:
            nel = 1
        if key > maxkey:
            maxkey = key
    # +100 hack making lut bigger just in case there are unknown labels
    if nel > 1:
        lut = np.zeros((maxkey + 100, nel), dtype=np.int32)
    else:
        lut = np.zeros((maxkey + 100), dtype=np.int32)
    for key, data in mapdict.items():
        try:
            lut[key] = data
        except IndexError:
            print("Wrong key ", key)
    # do the mapping
    if torch.is_tensor(label):
        lut = torch.from_numpy(lut)
    return lut[label]