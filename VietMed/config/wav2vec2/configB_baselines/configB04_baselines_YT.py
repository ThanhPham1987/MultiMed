"""
  Vietnamese Youtube (audiobook)
"""
import copy
import os.path
import pickle

from sisyphus import gs, tk, Path
from sisyphus.delayed_ops import DelayedFunction
import i6_core.am as am
import i6_core.corpus as corpus_recipes
import i6_core.features as features
import i6_core.rasr as rasr
from i6_core.returnn.config import ReturnnConfig
from i6_core.returnn.training import Checkpoint
from i6_core.meta.system import CorpusObject
from i6_private.users.vieting.helpers.alignment_cropping import cropping, cropping_out_type
#from i6_private.users.vieting.helpers.wav2vec2 import network as wav2vec2_network
from i6_private.users.dle.XLSR_Peter_8enc import network as wav2vec2_base_network
from i6_private.users.vieting.helpers.xlsr import network as xlsr_network
from i6_private.users.vieting.pipeline.nn_training import train
from i6_private.users.vieting.pipeline.system import HybridRecognitionSystem
from returnn_common.asr import gt

from config.configA_data.configA02_data_recog import get_recog_system_VietMed_dev_v1, get_recog_system_VietMed_test_v1
from i6_private.users.dle.data_augmentation.specaugment_Julian import SpecAugment, get_funcs

from returnn.import_ import import_
returnn_common = import_("github.com/rwth-i6/returnn_common", "models/base", "20210929-2243f105ba0befb2eba63f53a2350d4e26639532")
from returnn_import.github_com.rwth_i6.returnn_common.v20210929142536_2243f105ba0b.models.base import \
    LayerRef, LayerDictRaw, Module, get_root_extern_data
import returnn_import.github_com.rwth_i6.returnn_common.v20210929142536_2243f105ba0b.models._generated_layers as layers

filename_handle = os.path.splitext(os.path.basename(__file__))[0]
gs.ALIAS_AND_OUTPUT_SUBDIR = f"{filename_handle}/"
rasr.flow.FlowNetwork.default_flags = {"cache_mode": "task_dependent"}


class SpecAugmentStandalone(Module):
    """
    fairseq_asr masking settings:
    mask_prob: 0.5 -> ratio of frames to be masked
    mask_length: 10 -> length of each mask in time axis (default value)
    mask_channel_prob: 0.1 -> ratio of features to be masked
    mask_channel_length: 64 -> length of each mask in feature axis
    """

    def __init__(self, inp_name, min_mask_each_n_frames=20, max_mask_each_n_frames=20,
                 max_frames_per_mask=10, frames_per_mask_sampler='static',
                 min_feature_masks=640, max_feature_masks=640,
                 max_features_per_mask=64, features_per_mask_sampler='static'):
        super().__init__()
        self.specaugment = SpecAugment(min_mask_each_n_frames, max_mask_each_n_frames,
                                       max_frames_per_mask, frames_per_mask_sampler,
                                       min_feature_masks, max_feature_masks,
                                       max_features_per_mask, features_per_mask_sampler)
        self.inp_name = inp_name

    def forward(self):
        x = layers.Copy()(get_root_extern_data(self.inp_name), name="specaugment_in")
        return self.specaugment(x)
        
        
def get_returnn_datasets():
    dev_data = {
        "class": "MetaDataset",
        "context_window": {"classes": 1, "data": 500},
        "data_map": {"classes": ("hdf", "classes"), "data": ("ogg", "data")},
        "datasets": {
            "hdf": {
                "class": "HDFDataset",
                "files": [
                    "/work/asr3/luescher/hiwis/dle/VietMed_05-09-2023/wav2vec2_07-09-2023/i6_core/returnn/hdf/ReturnnDumpHDFJob.0j69wxgdNqb4/work/data.hdf"
                ],
            },
            "ogg": {
                "audio": {"features": "raw", "sample_rate": 8000},
                "class": "OggZipDataset",
                "fixed_random_seed": 1,
                "path": "/work/asr3/luescher/hiwis/dle/VietMed_05-09-2023/wav2vec2_07-09-2023/i6_core/returnn/oggzip/BlissToOggZipJob.LBhHi52644DI/output/out.ogg.zip",
                "seq_ordering": "sorted_reverse",
                "targets": None,
            },
        },
        "seq_order_control_dataset": "ogg",
    }
    train_data = {
        "class": "MetaDataset",
        "context_window": {"classes": 1, "data": 500},
        "data_map": {"classes": ("hdf", "classes"), "data": ("ogg", "data")},
        "datasets": {
            "hdf": {
                "class": "HDFDataset",
                "files": [
                    "/work/asr3/luescher/hiwis/dle/VietMed_05-09-2023/wav2vec2_07-09-2023/i6_core/returnn/hdf/ReturnnDumpHDFJob.pSYsNtqZEeKj/output/data.hdf"
                ],
            },
            "ogg": {
                "audio": {"features": "raw", "sample_rate": 8000},
                "class": "OggZipDataset",
                "partition_epoch": 6,
                "path": [
                    "/work/asr3/luescher/hiwis/dle/VietMed_05-09-2023/wav2vec2_07-09-2023/i6_core/returnn/oggzip/BlissToOggZipJob.zRkJDEwsoa2r/output/out.ogg.zip"
                ],
                "seq_ordering": "laplace:226",
                "targets": None,
            },
        },
        "seq_order_control_dataset": "ogg",
    }
    return {'train': {'meta': train_data}, 'cv': {'meta': dev_data}}


def get_w2v_returnn_config(data_train, data_dev, dropout_val, pretrained_checkpoint, wave_norm):
    network = copy.deepcopy(wav2vec2_base_network)

    if wave_norm:
        network["wave_norm"] = {"class": "norm", "axes": "T", "from": "data"}
        network["feature_extractor"]["from"] = "wave_norm"

    # For 8kHz input. Otherwise, the number of resulting representations does not fit the number of expected output CART labels
    network["feature_extractor"]["subnetwork"]["layer6"]["subnetwork"]["layer0"]["strides"] = (1,)

    # Adding Specaugment
    frame_aug_perc = 0.5 
    feature_aug_perc = 0.1
    
    specaugment = SpecAugmentStandalone(
        inp_name="dropout_input",
        min_mask_each_n_frames=int(10*(1/frame_aug_perc)),
        max_mask_each_n_frames=int(10*(1/frame_aug_perc)),
        max_frames_per_mask=10, frames_per_mask_sampler='static',
        min_feature_masks=int((768*feature_aug_perc)/76), max_feature_masks=int((768*feature_aug_perc)/76),
        max_features_per_mask=76, features_per_mask_sampler='static',
    )
    specaugment_dict = specaugment.make_root_net_dict()["specaugment"]
    specaugment_dict["from"] = "dropout_input"
    specaugment_dict["subnetwork"]["eval_layer"].pop("from")
    specaugment_dict["subnetwork"]["eval_layer"].pop("kind")
    network["specaugment"] = specaugment_dict
    network["encoder"]["from"] = "specaugment"
            
    # Dropout
    for i in range(7):
        network['feature_extractor']['subnetwork'][f'layer{i}']['subnetwork']['layer1']['dropout'] = dropout_val       
    #network["dropout_input"]["dropout"] = dropout_val
    for i in range(8):
        network["encoder"]["subnetwork"][f'layer{i}']['subnetwork']["dropout1"]["dropout"] = dropout_val
        network["encoder"]["subnetwork"][f'layer{i}']['subnetwork']["dropout2"]["dropout"] = dropout_val
        network["encoder"]["subnetwork"][f'layer{i}']['subnetwork']["dropout3"]["dropout"] = dropout_val
                  
    # Checkpoints preload
    checkpoint_preload = {
        "vietYoutube_XLSR_large_" + str(pretrained_checkpoint): {
            "filename": "/work/asr3/luescher/hiwis/dle/wav2vec_pytorch-to-returnn/checkpoints/youtubeViet_xlsr_large/youtubeViet_xlsr_large_" + str(pretrained_checkpoint),
            "ignore_missing": True,
            "init_for_train": True,
            "prefix": "",
        }
    }
                                  
    returnn_config = ReturnnConfig(
            dict(
                train=data_train,
                dev=data_dev,
                extern_data={"classes": {"dim": 4501, "sparse": True}, "data": {"dim": 1}},
                batching="random",
                batch_size={"classes": 1875, "data": 150000},
                optimizer={"class": "adam"},
                optimizer_epsilon=0.1,
                gradient_noise=0.1,
                
                # Finetune part            
                learning_rate=0.0001,
                newbob_multi_num_epochs=20,
                
                learning_rate_control="newbob_multi_epoch",
                learning_rate_control_min_num_epochs_per_new_lr=3,
                learning_rate_control_relative_error_relative_lr=True,
                #min_learning_rate=1e-06,
                newbob_learning_rate_decay=0.9,        
                newbob_multi_update_interval=1,
                use_tensorflow=True,
                cache_size="0",
                update_on_device=True,
                window=1,
                network=network,
                preload_from_files=checkpoint_preload,
            ),
            #python_prolog={
            #    "modules": "import tensorflow as tf\nimport sys\nsys.setrecursionlimit(2500)",
            #},
            python_prolog=[
                "import tensorflow as tf\nimport sys\nsys.setrecursionlimit(2500)",
            ] + get_funcs(),
            pprint_kwargs={"sort_dicts": False},
        )
    return copy.deepcopy(returnn_config)


def _rename_zip_to_txt_gz(path):
    return path.replace(".zip", ".txt.gz")


def xlsr_training(dropout_val, pretrained_checkpoint, wave_norm):
    epochs = [40,80,120,200]

    # XLSR with reduced number of layers
    datasets = get_returnn_datasets()
    datasets = copy.deepcopy(datasets)
    
    """
    for c in ["train", "cv"]:
        ogg_path = datasets[c]["meta"]["datasets"]["ogg"]["path"]
        datasets[c]["meta"]["datasets"]["ogg"]["path"] = [
            ogg_path, DelayedFunction(ogg_path, _rename_zip_to_txt_gz)]
    """
    
    returnn_config_tmp = get_w2v_returnn_config(
        datasets["train"]["meta"], datasets["cv"]["meta"], dropout_val, pretrained_checkpoint, wave_norm)
    name = "Cont_YT_cut_epoch_" + str(pretrained_checkpoint) + "--wave_norm_" + str(wave_norm) + "--Dropout_" + str(dropout_val)
    train_job = train(name, returnn_config_tmp, num_epochs=max(epochs), keep_epochs=epochs)
 
 
    recog_epochs = [200]        
    sys_HYKIST_test = get_recog_system_VietMed_dev_v1(wave_norm)
    
    sys_tmp_HYKIST_test = copy.deepcopy(sys_HYKIST_test)
    recog_args_HYKIST = dict(
        corpus="vietmed_corpus_dev_v1",
        train_corpus="train",
        prior_scale=0.7,
        lm_scale=10,
        rqmt={"gpu": 1, "time": 5, "mem": 32, "qsub_args": "-l qname=*1080*"},
        #lm_gc_job_rqmt={"mem": 16},
    )
    recog_args_tmp = copy.deepcopy(recog_args_HYKIST)
    recog_args_tmp.pop("prior_scale")
    recog_args_tmp.pop("lm_scale")
    for prior_scale, lm_scale, tdp_label_loop in [
        (0.8, 14.0, 3) #(0.7, 14.0, 3), 
    ]:
        recog_name = f"{name}-vietmed_corpus_dev_v1--prior_{prior_scale}-lm_{lm_scale}-tdp_label_loop_{tdp_label_loop}"    
        sys_tmp_HYKIST_test.crp["base"].acoustic_model_config.tdp["*"].loop = tdp_label_loop
        if wave_norm == True:
            sys_tmp_HYKIST_test.recognition(
                    recog_name, recog_epochs, train_job=train_job, flow="waveform", lm_scale=lm_scale, prior_scale=prior_scale,
                    optlm=True, **recog_args_tmp)   
        else:
            sys_tmp_HYKIST_test.recognition(
                    recog_name, recog_epochs, train_job=train_job, flow="waveform_scaled", lm_scale=lm_scale, prior_scale=prior_scale,
                    optlm=True, **recog_args_tmp)
    
    # Recog Test - no AM/LM opt
    sys_HYKIST_test = get_recog_system_VietMed_test_v1(wave_norm)
    
    sys_tmp_HYKIST_test = copy.deepcopy(sys_HYKIST_test)
    recog_args_HYKIST = dict(
        corpus="vietmed_corpus_test_v1",
        train_corpus="train",
        prior_scale=0.7,
        lm_scale=10,
        rqmt={"gpu": 1, "time": 5, "mem": 32, "qsub_args": "-l qname=*1080*"},
        #lm_gc_job_rqmt={"mem": 16},
    )
    recog_args_tmp = copy.deepcopy(recog_args_HYKIST)
    recog_args_tmp.pop("prior_scale")
    recog_args_tmp.pop("lm_scale")
    for prior_scale, lm_scale, tdp_label_loop in [ # (0.7, 14.116149488417742, 3.0),
        (0.8, 11.197425, 3) #(0.7, 14.0, 3), (0.7, 15.0, 3), (0.7, 16.0, 3)
    ]:
        recog_name = f"{name}-vietmed_corpus_test_v1--prior_{prior_scale}-lm_{lm_scale}-tdp_label_loop_{tdp_label_loop}"     
        sys_tmp_HYKIST_test.crp["base"].acoustic_model_config.tdp["*"].loop = tdp_label_loop
        if wave_norm == True:
            sys_tmp_HYKIST_test.recognition(
                    recog_name, recog_epochs, train_job=train_job, flow="waveform", lm_scale=lm_scale, prior_scale=prior_scale,
                    optlm=False, **recog_args_tmp)   
        else:
            sys_tmp_HYKIST_test.recognition(
                    recog_name, recog_epochs, train_job=train_job, flow="waveform_scaled", lm_scale=lm_scale, prior_scale=prior_scale,
                    optlm=False, **recog_args_tmp)  
       
                                 
# Run all
#xlsr_training(dropout_val=0.1, pretrained_checkpoint=300, wave_norm=True)
xlsr_training(dropout_val=0.05, pretrained_checkpoint=300, wave_norm=True)