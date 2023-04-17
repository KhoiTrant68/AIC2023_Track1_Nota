from detection.models.model import NPNet
from reids.fastreid.models.model import ReID
from trackers.botsort.bot_sort import BoTSORT

from trackers.multicam_tracker.cluster_track import MCTracker
from trackers.multicam_tracker.clustering import Clustering, ID_Distributor

from mmpose.apis import init_pose_model

from perspective_transform.model import PerspectiveTransform
from perspective_transform.calibration import calibration_position
from tools.utils import (_COLORS, get_reader_writer, finalize_cams, write_vids, write_results, 
                    visualize, update_result_lists, sources, result_paths, map_infos, write_map)

from mmpose.apis import init_pose_model

import cv2
import os
import time
import numpy as np
import argparse
import pdb


def run(args, conf_thres, iou_thres, sources, result_paths, perspective):
    assert len(sources) == len(result_paths[0]), 'length of sources and result_paths is different'
    # detection model initilaize
    NPNet.initialize()
    detection = NPNet()
    detection.conf_thres = conf_thres
    detection.iou_thres = iou_thres
    classes = detection.classes
    
    # reid model initilaize
    ReID.initialize(max_batch_size=args['max_batch_size'])
    reid = ReID()

    # pose estimation initialize
    config_file = '/workspace/np_app_AIC2023/configs/body/2d_kpt_sview_rgb_img/topdown_heatmap/crowdpose/hrnet_w32_crowdpose_256x192.py'
    checkpoint_file = 'https://download.openmmlab.com/mmpose/top_down/hrnet/hrnet_w32_crowdpose_256x192-960be101_20201227.pth'
    pose = init_pose_model(config_file, checkpoint_file, device='cuda:0')

    # trackers initialize
    # trackers = [BoTSORT(track_buffer=args['track_buffer'], max_batch_size=args['max_batch_size']) for i in range(len(sources))]
    trackers = []
    for i in range(len(sources)):
        trackers.append(BoTSORT(track_buffer=args['track_buffer'], max_batch_size=args['max_batch_size'], 
                            appearance_thresh=args['sct_appearance_thresh'], euc_thresh=args['sct_euclidean_thresh']))

    # perspective transform initialize
    calibrations = calibration_position[perspective]
    perspective_transforms = [PerspectiveTransform(c, map_infos[perspective]['size'], args['ransac_thresh']) for c in calibrations]

    # id_distributor and multi-camera tracker initialize
    clustering = Clustering(appearance_thresh=args['clt_appearance_thresh'], euc_thresh=args['clt_euclidean_thresh'],
                            match_thresh=0.999, map_size=map_infos[perspective]['size'])
    mc_tracker = MCTracker(appearance_thresh=args['mct_appearance_thresh'], match_thresh=0.8, map_size=map_infos[perspective]['size'])
    id_distributor = ID_Distributor()

    # get source imgs, video writers
    src_handlers = [get_reader_writer(s) for s in sources]
    results_lists = [[] for i in range(len(result_paths[0]))]  # make empty lists to store tracker outputs in MOT Format
    map_img = cv2.imread(map_infos[perspective]['source'])
    map_writer = cv2.VideoWriter(map_infos[perspective]['savedir'], cv2.VideoWriter_fourcc(*'mp4v'), 30, map_infos[perspective]['size'])

    total_frames = len(src_handlers[0][0])
    cur_frame = 0
    stop = False

    while True:
        # if cur_frame == 300: break
        imgs = []
        cur_frame += 1
        start = time.time()

        # first, run trackers each frame independently
        for (img_paths, writer), tracker, perspective_transform, result_list in zip(src_handlers, trackers, perspective_transforms, results_lists):
            if len(img_paths) == 0:
                stop = True
                break
            # print(img_paths[0])
            img = cv2.imread(img_paths.pop(0))
            dets = detection.run(img)  # run detection model
            online_targets = tracker.update(np.array(dets), img, reid, pose)  # run tracker
            perspective_transform.run(tracker)  # run perspective transform

            # assign global_id to each track for multi-camera tracking
            for t in tracker.tracked_stracks:
                t.t_global_id = id_distributor.assign_id()  # assign temporal global_id
            imgs.append(img)
        if stop: break
        # pdb.set_trace()
        # second, run multi-camera tracker using above trackers results
        groups = clustering.update(trackers, cur_frame)
        mc_tracker.update(trackers, groups)
        latency = time.time() - start

        # update result lists using updated trackers
        update_result_lists(trackers, results_lists, cur_frame)
        
        if args['write_vid']:
            write_vids(trackers, imgs, src_handlers, latency, pose, _COLORS, mc_tracker, cur_frame)
            map_img = write_map(trackers, map_img, map_writer, _COLORS, mc_tracker, cur_frame)
        
        print(f"video frame ({cur_frame}/{total_frames}) ({latency:.6f} s)")
        
        if cur_frame % 1000 == 0:
            write_results(results_lists, result_paths)
    
    finalize_cams(src_handlers)
    map_writer.release()
    # third, postprocess on final results
    # mc_tracker.postprocess(results_lists)  # todo

    # save results txt
    write_results(results_lists, result_paths)

    NPNet.finalize()
    ReID.finalize()
    print('Done')


if __name__ == '__main__':
    args = {
        'max_batch_size' : 16,  # maximum input batch size of reid model
        'track_buffer' : 150,  # the frames for keep lost tracks
        'with_reid' : True,  # whether to use reid model's out feature map at first association

        'sct_appearance_thresh' : 0.4,  # threshold of appearance feature cosine distance when do single-cam tracking
        'sct_euclidean_thresh' : 0.1,  # threshold of euclidean distance when do single-cam tracking

        'clt_appearance_thresh' : 0.25,  # threshold of appearance feature cosine distance when do multi-cam clustering
        'clt_euclidean_thresh' : 0.12,  # threshold of euclidean distance when do multi-cam clustering

        'mct_appearance_thresh' : 0.4,  # threshold of appearance feature cosine distance when do cluster tracking (not important)

        'ransac_thresh' : 10,  # threshold of ransac when find homography matrix 
        'frame_rate' : 30,  # your video(camera)'s fps
        'write_vid' : True,  # write result to video
        }

    run(args=args, conf_thres=0.1, iou_thres=0.45, sources=sources['S005'], result_paths=result_paths['S005'], perspective='S005')
    # run(args=args, conf_thres=0.1, iou_thres=0.45, sources=sources['S008'], result_paths=result_paths['S008'], perspective='S008')
    # run(args=args, conf_thres=0.1, iou_thres=0.45, sources=sources['S013'], result_paths=result_paths['S013'], perspective='S013')
    # run(args=args, conf_thres=0.1, iou_thres=0.45, sources=sources['S017'], result_paths=result_paths['S017'], perspective='S017')
    # run(args=args, conf_thres=0.1, iou_thres=0.45, sources=sources['S020'], result_paths=result_paths['S020'], perspective='S020')
