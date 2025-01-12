from yolov5.utils.general import (
    check_img_size, non_max_suppression, scale_coords, xyxy2xywh, xywh2xyxy)
from yolov5.utils.torch_utils import select_device
from yolov5.utils.datasets import letterbox
from yolov5 import YOLOv5

import matplotlib.pyplot as plt
import matplotlib

from action_recognition.DeepSORT_yolov5.utils_ds.parser import get_config
from action_recognition.DeepSORT_yolov5.utils_ds.draw import draw_boxes
from action_recognition.DeepSORT_yolov5.deep_sort import build_tracker

import argparse
import os
import time
import numpy as np
import warnings
import cv2
import torch
import torch.backends.cudnn as cudnn
from random import randint

import sys

currentUrl = os.path.dirname(__file__)
sys.path.append(os.path.abspath(os.path.join(currentUrl, 'yolov5')))

cudnn.benchmark = True

matplotlib.use('TkAgg')


def time_synchronized():
    torch.cuda.synchronize() if torch.cuda.is_available() else None
    return time.time()

def augment_deepsort_bbox(bbox, frame_width, frame_height, width_ratio=1.5, height_ratio=1.2):
    """
    :param bbox: the bounding box with format of [x, y, width, height]
    :param frame_width: the width of the frame that contains the bounding box
    :param frame_height: the height of the frame that contians the bounding box
    :param width_ratio: the augment ratio in width
    :param height_ratio: the augment ratio in height
    :return: an augmented bounding box with format of [x, y, width, height]
    """
    x_center = bbox[0] + bbox[2] / 2
    y_center = bbox[1] + bbox[3] / 2
    x_augmented = max(0, x_center - bbox[2] * width_ratio / 2)
    y_augmented = max(0, y_center - bbox[3] * height_ratio / 2)
    width_augmented = min(bbox[2] * width_ratio, frame_width - x_augmented)
    height_augmented = min(bbox[3] * height_ratio, frame_height - y_augmented)
    return np.array([x_augmented, y_augmented, width_augmented, height_augmented])


class VideoTracker(object):
    def __init__(self, args, yolo_detector):
        print('Initialize DeepSORT & YOLO-V5')
        # ***************** Initialize ******************************************************
        self.args = args

        self.img_size = args.img_size  # image size in detector, default is 640
        self.frame_interval = args.frame_interval  # frequency

        self.device = select_device(args.device)
        self.half = self.device.type != 'cpu'  # half precision only supported on CUDA

        # create video capture ****************
        if args.display:
            cv2.namedWindow("test", cv2.WINDOW_NORMAL)
            cv2.resizeWindow("test", args.display_width, args.display_height)

        if args.cam != -1:
            print("Using webcam " + str(args.cam))
            self.vdo = cv2.VideoCapture(args.cam)
            self.fps = self.vdo.get(cv2.CAP_PROP_FPS)
        else:
            self.vdo = cv2.VideoCapture()
            self.fps = self.vdo.get(cv2.CAP_PROP_FPS)

        # ***************************** initialize DeepSORT **********************************
        cfg = get_config()
        cfg.merge_from_file(args.config_deepsort)

        use_cuda = self.device.type != 'cpu' and torch.cuda.is_available()
        self.deepsort = build_tracker(cfg, use_cuda=use_cuda)

        # ***************************** initialize YOLO-V5 **********************************
        # self.detector = torch.load(args.weights, map_location=self.device)['model'].float()  # load to FP32
        # self.detector = YOLOv5(args.weights, 'cuda')
        # self.detector = torch.hub.load('ultralytics/yolov5', 'yolov5s', pretrained=True)
        self.detector = yolo_detector
        self.detector.to(self.device).eval()
        # if self.half:
        #     self.detector.half()  # to FP16

        self.names = self.detector.module.names if hasattr(self.detector, 'module') else self.detector.names

        print('Done..')
        if self.device == 'cpu':
            warnings.warn("Running in cpu mode which maybe very slow!", UserWarning)

    def __enter__(self):
        # ************************* Load video from camera *************************
        if self.args.cam != -1:
            print('Camera ...')
            ret, frame = self.vdo.read()
            assert ret, "Error: Camera error"
            self.im_width = int(self.vdo.get(cv2.CAP_PROP_FRAME_WIDTH))
            self.im_height = int(self.vdo.get(cv2.CAP_PROP_FRAME_HEIGHT))

        # ************************* Load video from file *************************
        else:
            assert os.path.isfile(self.args.input_path), "Path error"
            self.vdo.open(self.args.input_path)
            self.im_width = int(self.vdo.get(cv2.CAP_PROP_FRAME_WIDTH))
            self.im_height = int(self.vdo.get(cv2.CAP_PROP_FRAME_HEIGHT))
            assert self.vdo.isOpened()
            print('Done. Load video file ', self.args.input_path)

        # ************************* create output *************************
        if self.args.save_path:
            os.makedirs(self.args.save_path, exist_ok=True)
            # path of saved video and results
            self.save_video_path = os.path.join(self.args.save_path, "results.mp4")

            # create video writer
            fourcc = cv2.VideoWriter_fourcc(*self.args.fourcc)
            self.writer = cv2.VideoWriter(self.save_video_path, fourcc,
                                          self.vdo.get(cv2.CAP_PROP_FPS), (self.im_width, self.im_height))
            print('Done. Create output file ', self.save_video_path)

        if self.args.save_txt:
            os.makedirs(self.args.save_txt, exist_ok=True)

        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        self.vdo.release()
        self.writer.release()
        if exc_type:
            print(exc_type, exc_value, exc_traceback)

    def run(self):
        yolo_time, sort_time, avg_fps = [], [], []
        t_start = time.time()

        idx_frame = 0
        last_out = None

        videoFrames = []
        playerBoxes = {}
        colors = []
        frame_width = 0
        frame_height = 0

        while self.vdo.grab():
            # Inference *********************************************************************
            t0 = time.time()
            _, img0 = self.vdo.retrieve()

            videoFrames.append(img0)
            frame_height = img0.shape[0]
            frame_width = img0.shape[1]

            if idx_frame % self.args.frame_interval == 0:
                outputs, yt, st = self.image_track(img0)  # (#ID, 5) x1,y1,x2,y2,id
                last_out = outputs
                yolo_time.append(yt)
                sort_time.append(st)
                print('Frame %d Done. YOLO-time:(%.3fs) SORT-time:(%.3fs)' % (idx_frame, yt, st))

                for output in outputs:
                    bbox = output[0:4]
                    player_id = output[-1]

                    if player_id not in playerBoxes:
                        playerBoxes[player_id] = {}

                    playerBoxes[player_id][idx_frame] = bbox
            else:
                outputs = last_out  # directly use prediction in last frames

            idx_frame += 1

        # print('Avg YOLO time (%.3fs), Sort time (%.3fs) per frame' % (sum(yolo_time) / len(yolo_time),
        #                                                               sum(sort_time) / len(sort_time)))
        # t_end = time.time()
        # print('Total time (%.3fs), Total Frame: %d' % (t_end - t_start, idx_frame))

        playerBoxes_list = transform_playerBoxes_to_list(videoFrames, playerBoxes)

        if len(playerBoxes_list) != 0:
            num_of_players = playerBoxes_list[0].shape[0]
            for i in range(num_of_players):
                colors.append((randint(0, 255), randint(0, 255), randint(0, 255)))
        # self.fps = 30
        return videoFrames, playerBoxes_list, frame_width, frame_height, colors, self.fps

    def image_track(self, im0):
        """
        :param im0: original image, BGR format
        :return:
        """
        # preprocess ************************************************************
        # Padded resize
        img = letterbox(im0, new_shape=self.img_size)[0]
        # Convert
        img = img[:, :, ::-1].transpose(2, 0, 1)  # BGR to RGB, to 3x416x416
        img = np.ascontiguousarray(img)

        # numpy to tensor
        img = torch.from_numpy(img).to(self.device)
        img = img.half() if self.half else img.float()  # uint8 to fp16/32
        img /= 255.0  # 0 - 255 to 0.0 - 1.0
        if img.ndimension() == 3:
            img = img.unsqueeze(0)
        s = '%gx%g ' % img.shape[2:]  # print string

        # Detection time *********************************************************
        # Inference
        t1 = time_synchronized()
        with torch.no_grad():
            # results = self.detector.predict(img, augment=self.args.augment)  # list: bz * [ (#obj, 6)]
            # results = self.detector(img, augment=self.args.augment)  # list: bz * [ (#obj, 6)]
            results = self.detector(im0)  # list: bz * [ (#obj, 6)]
            res = results.xyxy[0]

        # Apply NMS and filter object other than person (cls:0)
        # pred = non_max_suppression(res.unsqueeze(0), self.args.conf_thres, self.args.iou_thres,
        #                            classes=self.args.classes, agnostic=self.args.agnostic_nms)
        pred = res[res[:, -1] == 0].unsqueeze(0)
        t2 = time_synchronized()

        # get all obj ************************************************************
        det = pred[0]  # for video, bz is 1
        if det is not None and len(det):  # det: (#obj, 6)  x1 y1 x2 y2 conf cls

            # Rescale boxes from img_size to original im0 size
            # det[:, :4] = scale_coords(img.shape[2:], det[:, :4], im0.shape).round()

            # Print results. statistics of number of each obj
            for c in det[:, -1].unique():
                n = (det[:, -1] == c).sum()  # detections per class
                s += '%g %ss, ' % (n, self.names[int(c)])  # add to string

            bbox_xywh = xyxy2xywh(det[:, :4]).cpu()
            confs = det[:, 4:5].cpu()

            # ****************************** deepsort ****************************
            outputs = self.deepsort.update(bbox_xywh, confs, im0)
            # (#ID, 5) x1,y1,x2,y2,track_ID
        else:
            outputs = torch.zeros((0, 5))

        t3 = time.time()
        return outputs, t2 - t1, t3 - t2

def transform_playerBoxes_to_list(videoFrames, playerBoxes):
    """
    :param videoFrames: The list of video frames
    :param playerBoxes: The dictionary of player's bbox.
    :return: A playerBoxes list that contains bbox for every frame
    """
    list_of_bbox = []
    player_ids = list(playerBoxes.keys())
    print("player ids: {}".format(player_ids))

    for i in range(len(videoFrames)):
        bboxes = [] # The bounding boxes for all players at the current frame
        for id in player_ids:
            bbox_dict = playerBoxes[id] # The dictionary with key to be frame idx, and value to be the bbox for the player at the frame
            if i not in bbox_dict:
                bboxes.append([0, 0, 0, 0])
            else:
                bbox = list(bbox_dict[i])
                # Do an initial bbox augment so that the top right point is unchanged but the
                # width and height of the bbox is augmented.
                bbox = [bbox[0], bbox[1], 1.5 * (bbox[2]-bbox[0]), 1.1 * (bbox[3]-bbox[1])]
                # Do a second bbox augmentation so that the center of the bbox is unchanged, but the
                # width and height of the bbox is augmented.
                # These 2 augmentations are simply engineering tuning to make the shot detector to be
                # more accurate.
                bbox = augment_deepsort_bbox(bbox, videoFrames[i].shape[1], videoFrames[i].shape[0])
                bboxes.append(list(bbox))
        list_of_bbox.append(np.array(bboxes))

    return list_of_bbox


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    # input and output
    parser.add_argument('--input_path', type=str, default='../../videos/test2.mp4', help='source')  # file/folder, 0 for webcam
    parser.add_argument('--save_path', type=str, default='output/', help='output folder')  # output folder
    parser.add_argument("--frame_interval", type=int, default=1)
    parser.add_argument('--fourcc', type=str, default='mp4v', help='output video codec (verify ffmpeg support)')
    parser.add_argument('--device', default='', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--save_txt', default='output/predict/', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')

    # camera only
    parser.add_argument("--display", action="store_true")
    parser.add_argument("--display_width", type=int, default=800)
    parser.add_argument("--display_height", type=int, default=600)
    parser.add_argument("--camera", action="store", dest="cam", type=int, default="-1")

    # YOLO-V5 parameters
    parser.add_argument('--weights', type=str, default='weights/yolov5s.pt', help='model.pt path')
    parser.add_argument('--img-size', type=int, default=640, help='inference size (pixels)')
    parser.add_argument('--conf-thres', type=float, default=0.5, help='object confidence threshold')
    parser.add_argument('--iou-thres', type=float, default=0.5, help='IOU threshold for NMS')
    parser.add_argument('--classes', nargs='+', type=int, default=[0], help='filter by class')
    parser.add_argument('--agnostic-nms', action='store_true', help='class-agnostic NMS')
    parser.add_argument('--augment', action='store_true', help='augmented inference')

    # deepsort parameters
    parser.add_argument("--config_deepsort", type=str, default="./configs/deep_sort.yaml")

    args = parser.parse_args()
    args.img_size = check_img_size(args.img_size)
    print(args)
    yolo_detector = torch.hub.load('ultralytics/yolov5', 'yolov5s', pretrained=True)

    with VideoTracker(args,yolo_detector) as vdo_trk:
        frames, playerBoxes, _, _, colors, fps = vdo_trk.run()
        print("frames size: {}".format(len(frames)))
        print("colors: {}, fps: {}".format(colors, fps))
        print("playerBoxes list len: {}, boxes list: {}".format(len(playerBoxes), playerBoxes))
