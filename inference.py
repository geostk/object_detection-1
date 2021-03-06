import tensorflow as tf
import cv2
import numpy as np
import os
import time
import argparse
from glob import glob
import functools
import utils.visualize as vis
from utils.bboxes import generate_anchors, bbox_decode
from utils.parse_config import parse_config


class Inference(object):
    def __init__(self, cfg_file):
        cfg = parse_config(cfg_file)
        self.data_cfg = cfg['data_config']
        self.train_cfg = cfg['train_config']
        self.model_cfg = cfg['model_config']
        self.infer_cfg = cfg['infer_config']
        self.col_channels = 3  # assume RGB channels only
        self.frozen_model_file = os.path.join(
            self.infer_cfg.model_dir, self.infer_cfg.frozen_model)
        self.img_h, self.img_w = self.infer_cfg.network_input_shape
        self.labels = self.get_labels()
        self.anchors = self.generate_anchors()
        if self.infer_cfg.input_type != 'http':
            self.network_tensors = self.network_forward_pass()

    def get_labels(self):
        labels = {}
        with open(self.data_cfg.out_labels, 'r') as f:
            for line in f:
                prod_id, prod_name = line.split(',')
                prod_name = (prod_name.split('\n')[0]).strip()
                try:
                    prod_id = int(prod_id)
                except ValueError:
                    continue
                labels[prod_id] = prod_name
        return labels

    def preprocess_image(self, image):
        # tensorflow expects RGB!
        image = image[:, :, :3]
        image = image[:, :, ::-1]
        # cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        h, w = image.shape[:2]
        patches = []
        for crop in self.infer_cfg.frame_crops:
            y1, x1, y2, x2 = crop
            y1, y2 = int(h * y1), int(h * y2)
            x1, x2 = int(h * x1), int(h * x2)
            patch = image[y1:y2, x1:x2]
            patch = cv2.resize(patch, (self.img_w, self.img_h),
                               interpolation=cv2.INTER_AREA)
            patches.append(patch)
        return patches

    def generate_anchors(self):
        all_anchors = []
        for i, (base_anchor_size, stride) in enumerate(zip(
                self.model_cfg.base_anchor_sizes,
                self.model_cfg.anchor_strides)):
            grid_shape = tf.constant(
                self.model_cfg.input_shape, tf.int32) / stride
            anchors = generate_anchors(
                grid_shape=grid_shape,
                base_anchor_size=base_anchor_size,
                stride=stride,
                scales=self.model_cfg.anchor_scales,
                aspect_ratios=self.model_cfg.anchor_ratios)
            all_anchors.append(anchors)
        return tf.concat(all_anchors, axis=0)

    def draw_bboxes_on_images(self, images, bbox_probs, bbox_regs):
        batch_size = len(self.infer_cfg.frame_crops)
        images = tf.cast(images, tf.uint8)
        images = tf.split(
            images,
            num_or_size_splits=batch_size,
            axis=0)
        bbox_probs = tf.split(
            tf.squeeze(bbox_probs),
            num_or_size_splits=batch_size,
            axis=0)
        bbox_regs = tf.split(
            bbox_regs,
            num_or_size_splits=batch_size,
            axis=0)
        out_images = []

        for i in range(batch_size):
            obj_prob = 1. - bbox_probs[i][:, 0]
            indices = tf.squeeze(tf.where(
                tf.greater(obj_prob, 0.)))

            def _draw_bboxes():
                img = tf.squeeze(images[i])
                bboxes = tf.gather(bbox_regs[i], indices)
                class_probs = tf.gather(bbox_probs[i], indices)
                # bboxes = tf.zeros_like(bboxes)
                anchors = tf.gather(self.anchors, indices)
                bboxes = bbox_decode(
                    bboxes, anchors, self.model_cfg.scale_factors)
                # bboxes = tf.expand_dims(bboxes, axis=0)
                scores = tf.gather(obj_prob, indices)
                selected_indices = tf.image.non_max_suppression(
                    bboxes, scores,
                    max_output_size=200,
                    iou_threshold=0.5)
                bboxes = tf.gather(bboxes, selected_indices)
                class_probs = tf.gather(class_probs, selected_indices)
                top_probs, top_classes = tf.nn.top_k(class_probs, 1)
                vis_fn = functools.partial(
                    vis.visualize_bboxes_on_image,
                    class_labels=self.labels
                )
                out_img = tf.py_func(
                    vis_fn,
                    [img, bboxes, top_classes, top_probs], tf.uint8)
                return tf.expand_dims(out_img, axis=0)
                # return tf.image.draw_bounding_boxes(
                #    images[i], bboxes)

            def _default():
                img = images[i]
                dummy = 255 * tf.ones((1, 320, self.img_w, 3), tf.uint8)
                return tf.concat([img, dummy], axis=1)

            out_image = tf.cond(
                tf.greater(tf.rank(indices), 0),
                true_fn=_draw_bboxes,
                false_fn=_default)
            out_images.append(out_image)
        out_images = tf.concat(out_images, axis=0)
        return out_images

    def get_bboxes_and_classes(self, bbox_probs, bbox_regs):
        batch_size = len(self.infer_cfg.frame_crops)
        bbox_probs = tf.split(
            tf.squeeze(bbox_probs),
            num_or_size_splits=batch_size,
            axis=0)
        bbox_regs = tf.split(
            bbox_regs,
            num_or_size_splits=batch_size,
            axis=0)
        all_bboxes, all_top_classes, all_top_probs = [], [], []

        for i in range(batch_size):
            obj_prob = 1. - bbox_probs[i][:, 0]
            indices = tf.squeeze(tf.where(
                tf.greater(obj_prob, 0.4)))

            def _get_bboxes():
                bboxes = tf.gather(bbox_regs[i], indices)
                class_probs = tf.gather(bbox_probs[i], indices)
                # bboxes = tf.zeros_like(bboxes)
                anchors = tf.gather(self.anchors, indices)
                bboxes = bbox_decode(
                    bboxes, anchors, self.model_cfg.scale_factors)
                # bboxes = tf.expand_dims(bboxes, axis=0)
                scores = tf.gather(obj_prob, indices)
                selected_indices = tf.image.non_max_suppression(
                    bboxes, scores,
                    max_output_size=30,
                    iou_threshold=0.4)
                bboxes = tf.gather(bboxes, selected_indices)
                scaling = tf.constant(
                    [self.img_h, self.img_w, self.img_h, self.img_w],
                    dtype=tf.float32)
                scaling = tf.expand_dims(scaling, axis=0)
                bboxes = bboxes * scaling
                class_probs = tf.gather(class_probs, selected_indices)
                top_probs, top_classes = tf.nn.top_k(
                    class_probs, self.infer_cfg.top_k)
                return bboxes, top_probs, top_classes

            def _default():
                bboxes = -1. * tf.ones([1, 4], tf.float32)
                top_probs = -1. * tf.ones([1, self.infer_cfg.top_k], tf.float32)
                top_classes = tf.zeros([1, self.infer_cfg.top_k], tf.int32)
                return bboxes, top_probs, top_classes

            bboxes, top_probs, top_classes = tf.cond(
                tf.greater(tf.rank(indices), 0),
                true_fn=_get_bboxes,
                false_fn=_default)
            all_bboxes.append(bboxes)
            all_top_classes.append(top_classes)
            all_top_probs.append(top_probs)
        all_bboxes = tf.concat(all_bboxes, axis=0)
        all_top_probs = tf.concat(all_top_probs, axis=0)
        all_top_classes = tf.concat(all_top_classes, axis=0)
        return [all_bboxes, all_top_classes, all_top_probs]

    def network_forward_pass(self):
        """Creates graph for network forward pass"""
        with tf.gfile.GFile(self.frozen_model_file, "rb") as f:
            graph_def = tf.GraphDef()
            graph_def.ParseFromString(f.read())

        with tf.get_default_graph().as_default() as g:
            tf.import_graph_def(graph_def)
        images = g.get_tensor_by_name('import/images:0')
        # images = tf.reshape(images, [-1, self.img_h, self.img_w, 3])
        bbox_classes = g.get_tensor_by_name('import/bbox_classes:0')
        bbox_regs = g.get_tensor_by_name('import/bbox_regs:0')
        with tf.device('/cpu:0'):
            bbox_on_images = self.draw_bboxes_on_images(
                images, bbox_classes, bbox_regs)
        return {'images': images,
                'bbox_on_images': bbox_on_images}

    def _run_inference(self, sess, images):
        t0 = time.time()
        t1 = time.time()
        print("************************* ", images.shape)

        bbox_on_images = sess.run(
            self.network_tensors['bbox_on_images'],
            feed_dict={self.network_tensors['images']: images})
        t2 = time.time()
        t3 = time.time()
        return bbox_on_images, [t0, t1, t2, t3]

    def display_output(self, bbox_on_images):
        n, h, w = bbox_on_images.shape[:3]
        sep = np.zeros((h, 10, 3), dtype=np.uint8)
        out = bbox_on_images[0]
        for i in range(1, n):
            out = np.concatenate([out, sep, bbox_on_images[i]], axis=1)
        out = cv2.cvtColor(out, cv2.COLOR_RGB2BGR)
        cv2.imshow('out', out)
        if cv2.waitKey(10000) == 27:  # Esc key to stop
            return 0
        elif cv2.waitKey(10000) & 0xFF == ord('q'):
            return 0
        return 1

    def get_model_api(self):
        with tf.gfile.GFile(self.frozen_model_file, "rb") as f:
            graph_def = tf.GraphDef()
            graph_def.ParseFromString(f.read())

        with tf.get_default_graph().as_default() as g:
            tf.import_graph_def(graph_def)
        tf_images = g.get_tensor_by_name('import/images:0')
        bbox_classes = g.get_tensor_by_name('import/bbox_classes:0')
        bbox_regs = g.get_tensor_by_name('import/bbox_regs:0')
        with tf.device('/cpu:0'):
            tf_predictions = self.get_bboxes_and_classes(
                bbox_classes, bbox_regs)
        sess = tf.Session()

        def model_api(input_images):
            images = np.array(input_images)
            bboxes, top_classes, top_probs = sess.run(
                tf_predictions,
                feed_dict={tf_images: images})
            return bboxes, top_classes, top_probs

        return model_api

    def run_inference(self):
        sess = tf.Session()
        stats = SpeedStats()
        input_type = self.infer_cfg.input_type
        if input_type == 'images':
            img_files = self.infer_cfg.images
            if not isinstance(img_files, list):
                img_files = glob(img_files)
            for img_file in img_files:
                image = cv2.imread(img_file)
                images = np.array(self.preprocess_image(image))
                bbox_on_images, t = self._run_inference(sess, images)
                stats.update(t)
                if not self.display_output(bbox_on_images):
                    break
        elif input_type == 'video':
            video_file = self.infer_cfg.video
            cap = cv2.VideoCapture(video_file)
            while cap.isOpened():
                ret, image = cap.read()
                if not ret:
                    break
                image = self.preprocess_image(image)
                out, bboxes, t = self._run_inference(sess, image)
                stats.update(t)
                if not self.display_output(image, out, bboxes):
                    break
            cap.release()
        elif input_type == 'camera':
            cam_url = self.infer_cfg.camera
            cap = cv2.VideoCapture(cam_url)
            while cap.isOpened():
                ret, image = cap.read()
                if not ret:
                    break
                images = np.array(self.preprocess_image(image))
                bbox_on_images, t = self._run_inference(sess, images)
                stats.update(t)
                delta_t = t[2] - t[0]
                time.sleep(max(self.infer_cfg.camera_capture_interval - delta_t, 0.5))
                if not self.display_output(bbox_on_images):
                    break
            cap.release()
        else:
            raise RuntimeError(
                "input type {} not supported".format(input_type))

        cv2.destroyAllWindows()
        stats.summarize()


class SpeedStats(object):
    def __init__(self):
        self.sum_t = [0., 0., 0.]
        self.n_skip = 3
        self.count = 0

    def update(self, t):
        if self.count > self.n_skip:
            self.sum_t[0] += t[1] - t[0]
            self.sum_t[1] += t[2] - t[1]
            self.sum_t[2] += t[3] - t[2]
        self.count += 1

    def summarize(self):
        print("Pre-processing time : {:5.2f} ms/image".format(
            self.sum_t[0] / (self.count - self.n_skip) * 1000))
        print("Network inference time : {:5.2f} ms/image".format(
            self.sum_t[1] / (self.count - self.n_skip) * 1000))
        print("Post-processing time : {:5.2f} ms/image".format(
            self.sum_t[2] / (self.count - self.n_skip) * 1000))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config_file', type=str,
                        default='./config.yaml', help='Config file')
    args = parser.parse_args()
    config_file = args.config_file
    assert os.path.exists(config_file), \
        "{} not found".format(config_file)
    infer = Inference(config_file)
    infer.run_inference()
