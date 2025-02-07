# coding=utf-8
"""
训练数据增强，主要是采样。利用随机截取训练图上的框来生成新的训练样本。同时要保证采样的样本能包含真实的目标。
采样之后，为了保持训练数据格式的一致性，还需要对标注的坐标信息做变换
@author: libo
"""
import numpy as np
import six
import math
from PIL import Image, ImageEnhance

from config import train_parameters
from utils import log_feed_image, bbox, sampler

def bbox_area(src_bbox):
    width = src_bbox.xmax - src_bbox.xmin
    height = src_bbox.ymax - src_bbox.ymin
    return width * height


def generate_sample(sampler):
    scale = np.random.uniform(sampler.min_scale, sampler.max_scale)
    aspect_ratio = np.random.uniform(sampler.min_aspect_ratio, sampler.max_aspect_ratio)
    aspect_ratio = max(aspect_ratio, (scale ** 2.0))
    aspect_ratio = min(aspect_ratio, 1 / (scale ** 2.0))

    bbox_width = scale * (aspect_ratio ** 0.5)
    bbox_height = scale / (aspect_ratio ** 0.5)
    xmin_bound = 1 - bbox_width
    ymin_bound = 1 - bbox_height
    xmin = np.random.uniform(0, xmin_bound)
    ymin = np.random.uniform(0, ymin_bound)
    xmax = xmin + bbox_width
    ymax = ymin + bbox_height
    sampled_bbox = bbox(xmin, ymin, xmax, ymax)
    return sampled_bbox


def jaccard_overlap(sample_bbox, object_bbox):
    if sample_bbox.xmin >= object_bbox.xmax or \
                    sample_bbox.xmax <= object_bbox.xmin or \
                    sample_bbox.ymin >= object_bbox.ymax or \
                    sample_bbox.ymax <= object_bbox.ymin:
        return 0
    intersect_xmin = max(sample_bbox.xmin, object_bbox.xmin)
    intersect_ymin = max(sample_bbox.ymin, object_bbox.ymin)
    intersect_xmax = min(sample_bbox.xmax, object_bbox.xmax)
    intersect_ymax = min(sample_bbox.ymax, object_bbox.ymax)
    intersect_size = (intersect_xmax - intersect_xmin) * (intersect_ymax - intersect_ymin)
    sample_bbox_size = bbox_area(sample_bbox)
    object_bbox_size = bbox_area(object_bbox)
    overlap = intersect_size / (sample_bbox_size + object_bbox_size - intersect_size)
    return overlap


def satisfy_sample_constraint(sampler, sample_bbox, bbox_labels):
    if sampler.min_jaccard_overlap == 0 and sampler.max_jaccard_overlap == 0:
        return True
    for i in range(len(bbox_labels)):
        object_bbox = bbox(bbox_labels[i][1], bbox_labels[i][2], bbox_labels[i][3], bbox_labels[i][4])
        overlap = jaccard_overlap(sample_bbox, object_bbox)
        if sampler.min_jaccard_overlap != 0 and overlap < sampler.min_jaccard_overlap:
            continue
        if sampler.max_jaccard_overlap != 0 and overlap > sampler.max_jaccard_overlap:
            continue
        return True
    return False


def generate_batch_samples(batch_sampler, bbox_labels):
    sampled_bbox = []
    index = []
    c = 0
    for sampler in batch_sampler:
        found = 0
        for i in range(sampler.max_trial):
            if found >= sampler.max_sample:
                break
            sample_bbox = generate_sample(sampler)
            if satisfy_sample_constraint(sampler, sample_bbox, bbox_labels):
                sampled_bbox.append(sample_bbox)
                found = found + 1
                index.append(c)
        c = c + 1
    return sampled_bbox


def clip_bbox(src_bbox):
    src_bbox.xmin = max(min(src_bbox.xmin, 1.0), 0.0)
    src_bbox.ymin = max(min(src_bbox.ymin, 1.0), 0.0)
    src_bbox.xmax = max(min(src_bbox.xmax, 1.0), 0.0)
    src_bbox.ymax = max(min(src_bbox.ymax, 1.0), 0.0)
    return src_bbox


def meet_emit_constraint(src_bbox, sample_bbox):
    center_x = (src_bbox.xmax + src_bbox.xmin) / 2
    center_y = (src_bbox.ymax + src_bbox.ymin) / 2
    if center_x >= sample_bbox.xmin and \
            center_x <= sample_bbox.xmax and \
            center_y >= sample_bbox.ymin and \
            center_y <= sample_bbox.ymax:
        return True
    return False


def transform_labels(bbox_labels, sample_bbox):
    proj_bbox = bbox(0, 0, 0, 0)
    sample_labels = []
    for i in range(len(bbox_labels)):
        sample_label = []
        object_bbox = bbox(bbox_labels[i][1], bbox_labels[i][2], bbox_labels[i][3], bbox_labels[i][4])
        if not meet_emit_constraint(object_bbox, sample_bbox):
            continue
        sample_width = sample_bbox.xmax - sample_bbox.xmin
        sample_height = sample_bbox.ymax - sample_bbox.ymin
        proj_bbox.xmin = (object_bbox.xmin - sample_bbox.xmin) / sample_width
        proj_bbox.ymin = (object_bbox.ymin - sample_bbox.ymin) / sample_height
        proj_bbox.xmax = (object_bbox.xmax - sample_bbox.xmin) / sample_width
        proj_bbox.ymax = (object_bbox.ymax - sample_bbox.ymin) / sample_height
        proj_bbox = clip_bbox(proj_bbox)
        if bbox_area(proj_bbox) > 0:
            sample_label.append(bbox_labels[i][0])
            sample_label.append(float(proj_bbox.xmin))
            sample_label.append(float(proj_bbox.ymin))
            sample_label.append(float(proj_bbox.xmax))
            sample_label.append(float(proj_bbox.ymax))
            sample_label.append(bbox_labels[i][5])
            sample_labels.append(sample_label)
    return sample_labels


# 裁剪图片
def crop_image(img, bbox_labels, sample_bbox, image_width, image_height):
    sample_bbox = clip_bbox(sample_bbox)
    xmin = int(sample_bbox.xmin * image_width)
    xmax = int(sample_bbox.xmax * image_width)
    ymin = int(sample_bbox.ymin * image_height)
    ymax = int(sample_bbox.ymax * image_height)
    sample_img = img.crop((xmin, ymin, xmax, ymax))
    sample_labels = transform_labels(bbox_labels, sample_bbox)
    return sample_img, sample_labels


# 调整图片大小
def resize_img(img, sampled_labels):
    target_size = train_parameters['input_size']
    ret = img.resize((target_size[1], target_size[2]), Image.ANTIALIAS)
    return ret


# 图像增强，亮度调整
def random_brightness(img):
    prob = np.random.uniform(0, 1)
    if prob < train_parameters['image_distort_strategy']['brightness_prob']:
        brightness_delta = train_parameters['image_distort_strategy']['brightness_delta']
        delta = np.random.uniform(-brightness_delta, brightness_delta) + 1
        img = ImageEnhance.Brightness(img).enhance(delta)
    return img


# 图像增强，对比度调整
def random_contrast(img):
    prob = np.random.uniform(0, 1)
    if prob < train_parameters['image_distort_strategy']['contrast_prob']:
        contrast_delta = train_parameters['image_distort_strategy']['contrast_delta']
        delta = np.random.uniform(-contrast_delta, contrast_delta) + 1
        img = ImageEnhance.Contrast(img).enhance(delta)
    return img


# 图像增强，饱和度调整
def random_saturation(img):
    prob = np.random.uniform(0, 1)
    if prob < train_parameters['image_distort_strategy']['saturation_prob']:
        saturation_delta = train_parameters['image_distort_strategy']['saturation_delta']
        delta = np.random.uniform(-saturation_delta, saturation_delta) + 1
        img = ImageEnhance.Color(img).enhance(delta)
    return img


# 图像增强，色度调整
def random_hue(img):
    prob = np.random.uniform(0, 1)
    if prob < train_parameters['image_distort_strategy']['hue_prob']:
        hue_delta = train_parameters['image_distort_strategy']['hue_delta']
        delta = np.random.uniform(-hue_delta, hue_delta)
        img_hsv = np.array(img.convert('HSV'))
        img_hsv[:, :, 0] = img_hsv[:, :, 0] + delta
        img = Image.fromarray(img_hsv, mode='HSV').convert('RGB')
    return img


# 概率的图像增强
def distort_image(img):
    prob = np.random.uniform(0, 1)
    # Apply different distort order
    if prob > 0.5:
        img = random_brightness(img)
        img = random_contrast(img)
        img = random_saturation(img)
        img = random_hue(img)
    else:
        img = random_brightness(img)
        img = random_saturation(img)
        img = random_hue(img)
        img = random_contrast(img)
    return img


def expand_image(img, bbox_labels, img_width, img_height):
    prob = np.random.uniform(0, 1)
    if prob < train_parameters['image_distort_strategy']['expand_prob']:
        expand_max_ratio = train_parameters['image_distort_strategy']['expand_max_ratio']
        if expand_max_ratio - 1 >= 0.01:
            expand_ratio = np.random.uniform(1, expand_max_ratio)
            height = int(img_height * expand_ratio)
            width = int(img_width * expand_ratio)
            h_off = math.floor(np.random.uniform(0, height - img_height))
            w_off = math.floor(np.random.uniform(0, width - img_width))
            expand_bbox = bbox(-w_off / img_width, -h_off / img_height,
                               (width - w_off) / img_width,
                               (height - h_off) / img_height)
            expand_img = np.uint8(np.ones((height, width, 3)) * np.array([127.5, 127.5, 127.5]))
            expand_img = Image.fromarray(expand_img)
            expand_img.paste(img, (int(w_off), int(h_off)))
            bbox_labels = transform_labels(bbox_labels, expand_bbox)
            return expand_img, bbox_labels, width, height
    return img, bbox_labels, img_width, img_height


def preprocess(img, bbox_labels, mode):
    img_width, img_height = img.size
    sampled_labels = bbox_labels
    if mode == 'train':
        if train_parameters['apply_distort']:
            img = distort_image(img)
        if train_parameters['apply_expand']:
            img, bbox_labels, img_width, img_height = expand_image(img, bbox_labels, img_width, img_height)

        if train_parameters['apply_corp']:
            batch_sampler = []
            # hard-code here
            batch_sampler.append(sampler(1, 1, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0))
            batch_sampler.append(sampler(1, 50, 0.3, 1.0, 0.5, 2.0, 0.1, 0.0))
            batch_sampler.append(sampler(1, 50, 0.3, 1.0, 0.5, 2.0, 0.3, 0.0))
            batch_sampler.append(sampler(1, 50, 0.3, 1.0, 0.5, 2.0, 0.5, 0.0))
            batch_sampler.append(sampler(1, 50, 0.3, 1.0, 0.5, 2.0, 0.7, 0.0))
            batch_sampler.append(sampler(1, 50, 0.3, 1.0, 0.5, 2.0, 0.9, 0.0))
            batch_sampler.append(sampler(1, 50, 0.3, 1.0, 0.5, 2.0, 0.0, 1.0))
            sampled_bbox = generate_batch_samples(batch_sampler, bbox_labels)
            if len(sampled_bbox) > 0:
                idx = int(np.random.uniform(0, len(sampled_bbox)))
                img, sampled_labels = crop_image(img, bbox_labels, sampled_bbox[idx], img_width, img_height)

        mirror = int(np.random.uniform(0, 2))
        if mirror == 1:
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
            for i in six.moves.xrange(len(sampled_labels)):
                tmp = sampled_labels[i][1]
                sampled_labels[i][1] = 1 - sampled_labels[i][3]
                sampled_labels[i][3] = 1 - tmp

    img = resize_img(img, sampled_labels)
    if train_parameters['log_feed_image']:
        log_feed_image(img, sampled_labels)
    img = np.array(img).astype('float32')
    img -= train_parameters['mean_rgb']
    img = img.transpose((2, 0, 1))  # HWC to CHW
    img *= 0.007843
    return img, sampled_labels