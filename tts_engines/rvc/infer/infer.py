import logging
import os
import sys
import time

import faiss
import librosa
import numpy as np
import soundfile as sf
import torch

import falltalkutils

now_dir = os.getcwd()
sys.path.append(now_dir)

from ..infer.pipeline import VC
from ..lib.tools.split_audio import process_audio, merge_audio
from ..lib.infer_pack.models import (
    SynthesizerTrnMs256NSFsid,
    SynthesizerTrnMs256NSFsid_nono,
    SynthesizerTrnMs768NSFsid,
    SynthesizerTrnMs768NSFsid_nono,
)
from ..configs.config import Config
from ..lib.utils import load_embedding

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class RVCParameters:
    def __init__(self):
        self.f0up_key = None
        self.filter_radius = None
        self.index_rate = None
        self.rms_mix_rate = None
        self.protect = None
        self.hop_length = None
        self.f0method = None
        self.split_audio = None
        self.f0autotune = None
        self.embedder_model = None
        self.training_data_size = None
        self.pth_path = None
        self.index_path = None
        self.index_filename = None
        self.index_filename_print = None
        self.index_size_print = None


class RVCPipeline:
    def __init__(self, device):
        self.config = Config(device)
        self.hubert_model = None
        self.tgt_sr = None
        self.net_g = None
        self.vc = None
        self.cpt = None
        self.version = None
        self.synth_version = None
        self.n_spk = None
        self.debug_rvc = True
        self.if_f0 = None
        self.file_index = None
        self.train_data = None
        self.data = None
        self.training_data_size = None
        self.d = None
        self.person = None

    def clean_up(self):
        del self.config
        del self.hubert_model
        del self.net_g
        del self.vc
        del self.cpt
        del self.n_spk
        del self.data
        del self.d
        del self.train_data
        torch.cuda.empty_cache()
        self.hubert_model = None
        self.tgt_sr = None
        self.net_g = None
        self.vc = None
        self.cpt = None
        self.version = None
        self.n_spk = None
        self.config = None
        self.synth_version = None
        self.if_f0 = None
        self.file_index = None
        self.train_data = None
        self.data = None
        self.training_data_size = None
        self.d = None
        self.person = None

    def load_hubert(self, embedder_model):
        models, _, _ = load_embedding(embedder_model)
        self.hubert_model = models[0]
        self.hubert_model = self.hubert_model.to(self.config.device)
        if self.config.is_half:
            print("loading hubert_model with half precision")
            self.hubert_model = self.hubert_model.half()
        else:
            print("loading hubert_model with full precision")
            self.hubert_model = self.hubert_model.float()
        self.hubert_model.eval()

    def load_index_file(self, file_index, training_data_size):
        if file_index is None:
            self.file_index = None
            self.data = None
            self.d = None
            self.train_data = None
        elif self.file_index != file_index:
            self.file_index = file_index
            index = faiss.read_index(self.file_index)
            self.d = index.d
            self.data = index.reconstruct_n(0, index.ntotal)

        if self.training_data_size != training_data_size and self.file_index is not None:
            self.training_data_size = training_data_size
            training_data_size_min = min(training_data_size, len(self.data))
            self.train_data = self.data[:training_data_size_min]

            if self.vc is not None:
                del self.vc
                self.vc = None

        if self.vc is None:
            if self.data is not None and self.d is not None and self.train_data is not None:
                self.vc = VC(self.tgt_sr, self.config, self.data, self.d, self.train_data, False)
            else:
                self.vc = VC(self.tgt_sr, self.config)

        if self.n_spk is None:
            self.n_spk = self.cpt["config"][-3]

    def load_synthesizer(self, if_f0):
        if self.if_f0 != if_f0:
            if self.version == "v1":
                if if_f0 == 1:
                    self.net_g = SynthesizerTrnMs256NSFsid(*self.cpt["config"], is_half=self.config.is_half)
                else:
                    self.net_g = SynthesizerTrnMs256NSFsid_nono(*self.cpt["config"])
            elif self.version == "v2":
                if if_f0 == 1:
                    self.net_g = SynthesizerTrnMs768NSFsid(*self.cpt["config"], is_half=self.config.is_half)
                else:
                    self.net_g = SynthesizerTrnMs768NSFsid_nono(*self.cpt["config"])

            self.if_f0 = if_f0
            self.net_g.load_state_dict(self.cpt["weight"], strict=False)
            self.net_g.eval().to(self.config.device)
            if self.config.is_half:
                self.net_g = self.net_g.half()
            else:
                self.net_g = self.net_g.float()

    def load_person(self, weight_root):
        if self.person != weight_root:
            self.person = weight_root
            self.cpt = torch.load(self.person, map_location="cpu")
            self.tgt_sr = self.cpt["config"][-1]
            self.cpt["config"][-3] = self.cpt["weight"]["emb_g.weight"].shape[0]
            if_f0 = self.cpt.get("f0", 1)
            self.version = self.cpt.get("version", "v1")
            self.load_synthesizer(if_f0)

    def voice_conversion(
            self,
            sid=0,
            input_audio_path=None,
            f0_up_key=None,
            f0_file=None,
            f0_method=None,
            file_index=None,
            index_rate=None,
            resample_sr=0,
            rms_mix_rate=None,
            protect=None,
            hop_length=None,
            output_path=None,
            split_audio=False,
            f0autotune=False,
            filter_radius=None,
            embedder_model=None,
    ):
        f0_up_key = int(f0_up_key)
        try:
            print(f"Loading audio from {input_audio_path}") if self.debug_rvc else None
            audio = falltalkutils.load_audio(input_audio_path, 16000)
            audio_max = np.abs(audio).max() / 0.95

            if audio_max > 1:
                audio /= audio_max

            if not self.hubert_model:
                print(f"Loading hubert model with {embedder_model}") if self.debug_rvc else None
                self.load_hubert(embedder_model)
            if_f0 = self.cpt.get("f0", 1)

            file_index = (
                file_index.strip(" ")
                .strip('"')
                .strip("\n")
                .strip('"')
                .strip(" ")
                .replace("trained", "added")
            )
            if self.tgt_sr != resample_sr >= 16000:
                self.tgt_sr = resample_sr
            if split_audio == "True":
                print("Splitting audio") if self.debug_rvc else None
                result, new_dir_path = process_audio(input_audio_path)
                if result == "Error":
                    return "Error with Split Audio", None
                dir_path = (
                    new_dir_path.strip(" ").strip('"').strip("\n").strip('"').strip(" ")
                )
                if dir_path != "":
                    paths = [
                        os.path.join(root, name)
                        for root, _, files in os.walk(dir_path, topdown=False)
                        for name in files
                        if name.endswith(".wav") and root == dir_path
                    ]
                try:
                    for path in paths:
                        self.voice_conversion(
                            sid,
                            path,
                            f0_up_key,
                            None,
                            f0_method,
                            file_index,
                            index_rate,
                            resample_sr,
                            rms_mix_rate,
                            protect,
                            hop_length,
                            path,
                            False,
                            f0autotune,
                            embedder_model,
                        )
                except Exception as error:
                    print(f"Error processing segmented audio: {error}")
                    return f"Error {error}"
                print("Finished processing segmented audio, now merging audio...") if self.debug_rvc else None
                merge_timestamps_file = os.path.join(
                    os.path.dirname(new_dir_path),
                    f"{os.path.basename(input_audio_path).split('.')[0]}_timestamps.txt",
                )
                self.tgt_sr, audio_opt = merge_audio(merge_timestamps_file)
                os.remove(merge_timestamps_file)

            else:
                print("Processing audio with VC pipeline") if self.debug_rvc else None
                audio_opt = self.vc.pipeline(
                    self.hubert_model,
                    self.net_g,
                    sid,
                    audio,
                    input_audio_path,
                    f0_up_key,
                    f0_method,
                    file_index,
                    index_rate,
                    if_f0,
                    filter_radius,
                    self.tgt_sr,
                    resample_sr,
                    rms_mix_rate,
                    self.version,
                    protect,
                    hop_length,
                    f0autotune,
                    f0_file=f0_file,
                )

            # Resample the audio to the target sample rate before saving
            if self.tgt_sr != resample_sr and resample_sr >= 16000 and resample_sr:
                print(f"Resampling audio from {self.tgt_sr} to {resample_sr}") if self.debug_rvc else None
                audio_opt = librosa.resample(audio_opt, self.tgt_sr, resample_sr)
                self.tgt_sr = resample_sr

            if output_path is not None:
                print(f"Saving file to {output_path}") if self.debug_rvc else None
                sf.write(output_path, audio_opt, self.tgt_sr, format="WAV")
                print(f"File saved to {output_path}") if self.debug_rvc else None

            return (self.tgt_sr, audio_opt)

        except Exception as error:
            print(f"Error during voice conversion: {error}")
            return None, None

    def get_vc(self, weight_root, sid, file_index=None, training_data_size=10000, debug_rvc=True):
        if debug_rvc:
            print(f"[Debug] Loading model checkpoint")
        self.load_person(weight_root)
        self.load_index_file(file_index, training_data_size)
        return self.vc

    def infer_pipeline(
            self,
            f0up_key,
            filter_radius,
            index_rate,
            rms_mix_rate,
            protect,
            hop_length,
            f0method,
            audio_input_path,
            audio_output_path,
            model_path,
            index_path,
            split_audio,
            f0autotune,
            embedder_model,
            training_data_size,
            debug_rvc,
    ):
        self.debug_rvc = debug_rvc
        if index_path is None or index_path.strip() == "":
            file_index = None
        else:
            file_index = index_path
        self.get_vc(model_path, 0, file_index, training_data_size, debug_rvc)

        try:
            start_time = time.time()
            self.voice_conversion(
                sid=0,
                input_audio_path=audio_input_path,
                f0_up_key=f0up_key,
                f0_file=None,
                f0_method=f0method,
                file_index=index_path,
                index_rate=float(index_rate),
                rms_mix_rate=float(rms_mix_rate),
                protect=float(protect),
                hop_length=hop_length,
                output_path=audio_output_path,
                split_audio=split_audio,
                f0autotune=f0autotune,
                filter_radius=filter_radius,
                embedder_model=embedder_model,
            )

            end_time = time.time()
            elapsed_time = end_time - start_time
            falltalkutils.logger.debug(f"Conversion completed. Output file: '{audio_output_path}' in {elapsed_time:.2f} seconds.") if debug_rvc else None

        except Exception as error:
            falltalkutils.logger.exception(f"Voice conversion failed: {error}")
