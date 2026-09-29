import base64
from datetime import datetime
import os
import subprocess
import tempfile
import time
import urllib.request
import torch

from PIL import Image
import whisperx
import streamlit as st

st.set_page_config(
    page_title="대본 기반 자막 자동 정렬 & 영상 합성 앱", layout="wide"
)

# 셀렉트박스 텍스트 커서(I-beam) 제거 및 포인터 커서 스타일 지정
st.markdown(
    """
    <style>
    div[data-baseweb="select"] input {
        cursor: pointer !important;
        caret-color: transparent !important;
    }
    div[data-baseweb="select"] {
        cursor: pointer !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🎬 대본 자막 자동 정렬 & 영상 합성 자동화")
st.write(
    "영상을 업로드하고 대본을 입력한 후, 자막 스타일을 자유롭게 맞춤 설정하세요."
)

# 세션 상태 초기화
if "final_video_bytes" not in st.session_state:
    st.session_state["final_video_bytes"] = None

if "processing" not in st.session_state:
    st.session_state["processing"] = False

if "history" not in st.session_state:
    st.session_state["history"] = []

if "success_time" not in st.session_state:
    st.session_state["success_time"] = None

if "last_uploaded_file_id" not in st.session_state:
    st.session_state["last_uploaded_file_id"] = None

is_disabled = st.session_state["processing"]

# 한글 폰트 보장 다운로드 로직 (서버 한글 깨짐 완전 방지)
FONT_DIR = os.path.join(os.getcwd(), "fonts")
FONT_PATH = os.path.join(FONT_DIR, "NanumGothic.ttf")


def ensure_font_exists():
    if not os.path.exists(FONT_DIR):
        os.makedirs(FONT_DIR, exist_ok=True)
    if not os.path.exists(FONT_PATH) or os.path.getsize(FONT_PATH) < 10000:
        url = "https://cdn.jsdelivr.net/gh/google/fonts/ofl/nanumgothic/NanumGothic-Bold.ttf"
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"}
            )
            with (
                urllib.request.urlopen(req) as response,
                open(FONT_PATH, "wb") as out_file,
            ):
                out_file.write(response.read())
        except Exception:
            pass


ensure_font_exists()


# Hex 색상을 ASS 포맷 색상(&H00BBGGRR)으로 변환
def hex_to_ass_color(hex_color):
    hex_color = hex_color.lstrip("#")
    r = hex_color[0:2]
    g = hex_color[2:4]
    b = hex_color[4:6]
    return f"&H00{b}{g}{r}"


# ASS 포맷 시간 변환 (H:MM:SS.cs)
def format_ass_time(seconds):
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    cs = int((seconds - int(seconds)) * 100)
    return f"{hrs}:{mins:02d}:{secs:02d}.{cs:02d}"


# 글꼴 크기 기반 어절 단위 가변 줄바꿈 함수
def wrap_korean_text(text, max_chars):
    if len(text) <= max_chars:
        return text
    words = text.split()
    if len(words) <= 1:
        return text

    lines = []
    current_line = []
    current_len = 0

    for word in words:
        word_len = len(word)
        added_len = word_len + (1 if current_line else 0)
        if current_len + added_len <= max_chars:
            current_line.append(word)
            current_len += added_len
        else:
            if current_line:
                lines.append(" ".join(current_line))
            current_line = [word]
            current_len = word_len

    if current_line:
        lines.append(" ".join(current_line))

    return "\n".join(lines)


# 전경 글자 선명도 유지 + 배경 은은한 글로우 이중 ASS 자막 생성 함수
def generate_ass_file(
    ass_filename,
    font_name,
    font_size,
    primary_hex,
    outline_hex,
    glow_hex,
    outline_w,
    glow_sat,
    glow_d,
    margin_v,
    segments_data,
    video_aspect=9.0 / 16.0,
):
    actual_font = "NanumGothic"

    scale = 4.0
    s_font = font_size * scale
    s_outline = outline_w * scale
    s_glow = glow_d * scale
    s_margin = margin_v * scale

    c_primary = hex_to_ass_color(primary_hex)
    c_outline = hex_to_ass_color(outline_hex)
    c_glow = hex_to_ass_color(glow_hex)

    glow_alpha_val = int((10 - glow_sat) * 25.5)
    glow_alpha_hex = f"&H{glow_alpha_val:02X}&"

    play_res_y = 1920
    play_res_x = int(play_res_y * video_aspect)

    ass_header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_res_x}
PlayResY: {play_res_y}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{actual_font},{s_font:.1f},{c_primary},&H00000000,{c_outline},&H00000000,-1,0,0,0,100,100,0,0,1,{s_outline:.1f},0,2,13,13,{s_margin:.1f},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for item in segments_data:
        t_start = format_ass_time(item["start"])
        t_end = format_ass_time(item["end"])
        ass_text = item["text"].replace("\n", r"\N")

        if glow_d > 0 and glow_sat > 0:
            blur_px = s_glow * 2.5
            glow_bord = s_outline + (s_glow * 1.5)
            glow_tag = f"{{\\an2\\blur{blur_px:.1f}\\bord{glow_bord:.1f}\\3c{c_glow}\\3a{glow_alpha_hex}\\1a&HFF&}}"
            events.append(
                f"Dialogue: 0,{t_start},{t_end},Default,,0,0,0,,{glow_tag}{ass_text}"
            )

        fg_tag = f"{{\\an2\\blur0\\bord{s_outline:.1f}\\3c{c_outline}\\1c{c_primary}\\1a&H00&}}"
        events.append(
            f"Dialogue: 1,{t_start},{t_end},Default,,0,0,0,,{fg_tag}{ass_text}"
        )

    with open(ass_filename, "w", encoding="utf-8") as f:
        f.write(ass_header + "\n".join(events))


col_left, col_right = st.columns([2, 1])

header_color = "#a3a8b4" if is_disabled else "#31333f"

with col_left:
    st.subheader("1. 영상 및 대본 입력")
    uploaded_file = st.file_uploader(
        "영상 파일(MP4, MOV 등)을 선택하세요",
        type=["mp4", "mov", "m4v", "mkv"],
        disabled=is_disabled,
    )

    # 새로운 영상이 업로드되거나 변경되면 이전 합성 완료 영상 제거
    if uploaded_file is not None:
        file_id = f"{uploaded_file.name}_{uploaded_file.size}"
        if st.session_state["last_uploaded_file_id"] != file_id:
            st.session_state["last_uploaded_file_id"] = file_id
            st.session_state["final_video_bytes"] = None
            st.session_state["success_time"] = None
    else:
        if st.session_state["last_uploaded_file_id"] is not None:
            st.session_state["last_uploaded_file_id"] = None
            st.session_state["final_video_bytes"] = None
            st.session_state["success_time"] = None

    script_text = st.text_area(
        "대본을 입력하세요 (줄바꿈 기준으로 자막 단위가 나뉩니다)",
        height=140,
        placeholder=(
            "여기에 대사를 입력하세요.\n줄바꿈을 기준으로 자막 화면이"
            " 분할됩니다."
        ),
        disabled=is_disabled,
    )

    st.divider()

    thumb_b64 = None
    preview_aspect_ratio = "9 / 16"
    video_aspect = 9.0 / 16.0

    if uploaded_file is not None:
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp_v:
                tmp_v.write(uploaded_file.getvalue())
                tmp_v_path = tmp_v.name

            tmp_img_path = tmp_v_path + "_thumb.jpg"
            ffmpeg_thumb_cmd = [
                "ffmpeg",
                "-y",
                "-ss",
                "00:00:01",
                "-i",
                tmp_v_path,
                "-vframes",
                "1",
                "-q:v",
                "2",
                tmp_img_path,
            ]
            subprocess.run(
                ffmpeg_thumb_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            if os.path.exists(tmp_img_path):
                try:
                    with Image.open(tmp_img_path) as img:
                        img_w, img_h = img.size
                        if img_w > 0 and img_h > 0:
                            preview_aspect_ratio = f"{img_w} / {img_h}"
                            video_aspect = img_w / img_h
                except Exception:
                    pass

                with open(tmp_img_path, "rb") as img_f:
                    thumb_b64 = base64.b64encode(img_f.read()).decode("utf-8")
                os.remove(tmp_img_path)
            os.remove(tmp_v_path)
        except Exception:
            thumb_b64 = None

    st.subheader("2. 🎨 자막 스타일 설정 & 미리보기")

    col_s1, col_s2, col_s3 = st.columns([1, 1, 1])

    with col_s1:
        st.markdown(
            f"<p style='color:{header_color}; font-weight:bold; margin-bottom:4px;'>글꼴 (Font)</p>",
            unsafe_allow_html=True,
        )
        font_option = st.selectbox(
            "글꼴 (Font)",
            [
                "NanumGothic",
                "Malgun Gothic",
                "NanumMyeongjo",
                "NanumSquare",
                "Pretendard",
                "Gmarket Sans",
                "Black Han Sans",
                "Dotum",
                "Batang",
            ],
            index=0,
            disabled=is_disabled,
            label_visibility="collapsed",
        )

        primary_color = st.color_picker(
            "글자 색상",
            value="#FFFFFF",
            disabled=is_disabled,
        )

        font_size = st.slider(
            "글꼴 크기",
            min_value=10,
            max_value=80,
            value=24,
            step=1,
            disabled=is_disabled,
        )

        st.markdown(
            f"<p style='color:{header_color}; font-weight:bold; margin-top:12px; margin-bottom:4px;'>하단 여백 (Margin Bottom)</p>",
            unsafe_allow_html=True,
        )
        margin_v = st.slider(
            "하단 여백 (Margin Bottom)",
            min_value=10,
            max_value=100,
            value=50,
            step=5,
            disabled=is_disabled,
            label_visibility="collapsed",
        )

    with col_s2:
        st.markdown(
            f"<p style='color:{header_color}; font-weight:bold; margin-bottom:8px;'>글꼴 획 (Outer Outline)</p>",
            unsafe_allow_html=True,
        )
        outline_color = st.color_picker(
            "획 색상", value="#000000", disabled=is_disabled
        )
        outline_width = st.slider(
            "획 두께 (바깥 기준)",
            min_value=0,
            max_value=10,
            value=2,
            step=1,
            disabled=is_disabled,
        )

        st.markdown(
            f"<p style='color:{header_color}; font-weight:bold; margin-top:16px; margin-bottom:8px;'>글꼴 글로우 (Glow)</p>",
            unsafe_allow_html=True,
        )
        glow_color = st.color_picker(
            "글로우 색상", value="#FF3366", disabled=is_disabled
        )
        glow_sat = st.slider(
            "글로우 채도",
            min_value=0,
            max_value=10,
            value=5,
            step=1,
            disabled=is_disabled,
        )
        glow_distance = st.slider(
            "글로우 범위",
            min_value=0,
            max_value=10,
            value=3,
            step=1,
            disabled=is_disabled,
        )

    with col_s3:
        st.markdown(
            f"<p style='color:{header_color}; font-weight:bold; margin-bottom:8px;'>👁️ 실시간 자막 미리보기</p>",
            unsafe_allow_html=True,
        )
        sample_lines = (
            [l.strip() for l in script_text.split("\n") if l.strip()]
            if script_text
            else []
        )
        raw_sample = sample_lines[0] if sample_lines else "자막 스타일 미리보기 예시"

        aspect_multiplier = video_aspect / (9.0 / 16.0)
        calc_max_chars = max(3, int((250 / font_size) * aspect_multiplier))
        preview_sample_text = wrap_korean_text(
            raw_sample, max_chars=calc_max_chars
        ).replace("\n", "<br/>")

        if thumb_b64:
            bg_css = (
                f"background-image: url('data:image/jpeg;base64,{thumb_b64}');"
                " background-size: cover; background-position: center;"
            )
        else:
            bg_css = "background-color: #1a1a1a;"

        glow_alpha_css = glow_sat / 10.0
        r_g = int(glow_color[1:3], 16)
        g_g = int(glow_color[3:5], 16)
        b_g = int(glow_color[5:7], 16)

        preview_html = f"""
        <div style="
            {bg_css}
            width: 100%;
            max-width: 280px;
            aspect-ratio: {preview_aspect_ratio};
            border-radius: 12px;
            border: 2px solid #444;
            margin: 0 auto;
            position: relative;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            justify-content: flex-end;
            align-items: center;
            padding-bottom: {margin_v * 0.4}px;
            box-sizing: border-box;
        ">
            <span style="
                font-family: '{font_option}', sans-serif;
                font-size: {font_size * 0.55}px;
                color: {primary_color};
                -webkit-text-stroke: {outline_width * 0.8}px {outline_color};
                paint-order: stroke fill;
                text-shadow: 0 0 {glow_distance * 2}px rgba({r_g}, {g_g}, {b_g}, {glow_alpha_css}), 0 0 {glow_distance * 4}px rgba({r_g}, {g_g}, {b_g}, {glow_alpha_css});
                font-weight: bold;
                text-align: center;
                line-height: 1.35;
                padding: 0 10px;
                word-break: keep-all;
            ">
                {preview_sample_text}
            </span>
        </div>
        """
        st.markdown(preview_html, unsafe_allow_html=True)

    st.divider()

    start_btn = st.button(
        "🚀 자막 정렬 및 영상 합성 시작",
        use_container_width=True,
        disabled=is_disabled,
    )
    cancel_btn = st.button(
        "합성 취소", use_container_width=True, disabled=not is_disabled
    )

    status_area = st.container()

    st.markdown(
        "<p style='color:#888888; font-weight:bold; margin-top:16px;"
        " margin-bottom:8px;'>📜 합성 이력 (최신 10개)</p>",
        unsafe_allow_html=True,
    )
    if st.session_state["history"]:
        for h_item in st.session_state["history"]:
            st.markdown(
                f"<p style='color:#888888; font-size:14px; margin:2px 0;'>• {h_item}</p>",
                unsafe_allow_html=True,
            )
    else:
        st.markdown(
            "<p style='color:#888888; font-size:14px;'>아직 완료된 합성 이력이 없습니다.</p>",
            unsafe_allow_html=True,
        )

with col_right:
    st.subheader("📺 합성 완료 영상")
    if st.session_state["final_video_bytes"] is not None:
        st.video(st.session_state["final_video_bytes"])
        st.download_button(
            label="🎬 자막 합성 영상(MP4) 다운로드",
            data=st.session_state["final_video_bytes"],
            file_name="captioned_video.mp4",
            mime="video/mp4",
            use_container_width=True,
            disabled=False,
        )
    else:
        st.info(
            "좌측에서 설정을 마치고 [합성 시작] 버튼을 누르면 이 위치에 최종 영상이 출력됩니다."
        )

    if st.session_state["success_time"] is not None:
        if time.time() - st.session_state["success_time"] < 10:
            st.success("🎉 자막 영상 합성이 완료되었습니다!")

# 취소 버튼 동작
if cancel_btn:
    st.session_state["processing"] = False
    st.rerun()

# 새로 합성 시작 시 기존 완성 영상 비우기 및 상태 초기화
if start_btn:
    st.session_state["final_video_bytes"] = None
    st.session_state["success_time"] = None
    st.session_state["processing"] = True
    st.rerun()

# 합성 작업 진행 로직
if st.session_state["processing"]:
    with status_area:
        if uploaded_file is None:
            st.error("영상 파일을 업로드해주세요!")
            st.session_state["processing"] = False
            st.rerun()
        elif not script_text.strip():
            st.error("대본을 입력해주세요!")
            st.session_state["processing"] = False
            st.rerun()
        else:
            video_filename = "temp_input.mp4"
            ass_filename = "temp_subtitles.ass"
            output_filename = "captioned_output.mp4"

            with st.spinner(
                "1/2단계: VAD 음성 분석 및 대사 타임스탬프 강제 정렬 진행 중..."
            ):
                try:
                    with open(video_filename, "wb") as f:
                        f.write(uploaded_file.read())

                    audio = whisperx.load_audio(video_filename)
                    audio_duration = len(audio) / 16000.0

                    device = "cuda" if torch.cuda.is_available() else "cpu"

                    vad_segments = []
                    try:
                        if hasattr(whisperx, "load_vad_model"):
                            vad_model = whisperx.load_vad_model(device)
                            vad_res = vad_model({
                                "waveform": torch.from_numpy(audio).unsqueeze(0),
                                "sample_rate": 16000,
                            })
                            if isinstance(vad_res, list):
                                vad_segments = vad_res
                            elif isinstance(vad_res, dict) and "segments" in vad_res:
                                vad_segments = vad_res["segments"]
                    except Exception:
                        vad_segments = []

                    align_model, metadata = whisperx.load_align_model(
                        language_code="ko", device=device
                    )

                    lines = [
                        line.strip()
                        for line in script_text.strip().split("\n")
                        if line.strip()
                    ]
                    full_text = " ".join(lines)

                    custom_segments = [{
                        "text": full_text,
                        "start": 0.0,
                        "end": audio_duration,
                    }]

                    result = whisperx.align(
                        custom_segments,
                        align_model,
                        metadata,
                        audio,
                        device,
                        return_char_alignments=False,
                    )
                    word_segments = result.get("word_segments", [])

                    def is_in_vad(t_start, t_end):
                        if not vad_segments:
                            return True
                        for v_seg in vad_segments:
                            v_start = v_seg.get("start", 0)
                            v_end = v_seg.get("end", 0)
                            if not (t_end < v_start or t_start > v_end):
                                return True
                        return False

                    segments_data = []
                    word_cursor = 0

                    aspect_multiplier = video_aspect / (9.0 / 16.0)
                    calc_max_chars = max(3, int((250 / font_size) * aspect_multiplier))

                    for line in lines:
                        line_words = line.split()
                        matched_words = []

                        for _ in range(len(line_words)):
                            if word_cursor < len(word_segments):
                                w = word_segments[word_cursor]
                                if (
                                    "start" in w
                                    and "end" in w
                                    and (w["end"] - w["start"]) > 0.05
                                ):
                                    if is_in_vad(w["start"], w["end"]) and w.get("score", 1.0) >= 0.2:
                                        matched_words.append(w)
                                word_cursor += 1

                        if matched_words:
                            start_val = matched_words[0]["start"]
                            end_val = matched_words[-1]["end"]
                            formatted_line = wrap_korean_text(
                                line, max_chars=calc_max_chars
                            )
                            segments_data.append({
                                "start": start_val,
                                "end": end_val,
                                "text": formatted_line,
                            })

                    if not segments_data:
                        st.error(
                            "사람 대사 음성을 매칭하지 못했습니다. 대본 및 영상 음성을 확인해 주세요."
                        )
                        st.session_state["processing"] = False
                        st.stop()

                    generate_ass_file(
                        ass_filename=ass_filename,
                        font_name=font_option,
                        font_size=font_size,
                        primary_hex=primary_color,
                        outline_hex=outline_color,
                        glow_hex=glow_color,
                        outline_w=outline_width,
                        glow_sat=glow_sat,
                        glow_d=glow_distance,
                        margin_v=margin_v,
                        segments_data=segments_data,
                        video_aspect=video_aspect,
                    )

                except Exception as e:
                    st.error(f"자막 정렬 중 오류가 발생했습니다: {e}")
                    st.session_state["processing"] = False
                    st.stop()

            with st.spinner(
                "2/2단계: 선명한 글자 + 은은한 글로우 레이어 FFmpeg 합성 중..."
            ):
                try:
                    ensure_font_exists()
                    fonts_dir_clean = os.path.abspath(FONT_DIR).replace("\\", "/")
                    ass_filename_clean = ass_filename.replace("\\", "/")

                    ffmpeg_cmd = [
                        "ffmpeg",
                        "-y",
                        "-i",
                        video_filename,
                        "-vf",
                        f"subtitles={ass_filename_clean}:fontsdir='{fonts_dir_clean}'",
                        "-c:a",
                        "copy",
                        output_filename,
                    ]

                    process = subprocess.run(
                        ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                    )

                    if process.returncode != 0:
                        st.error(f"FFmpeg 합성 오류: {process.stderr}")
                        st.session_state["processing"] = False
                        st.stop()

                    with open(output_filename, "rb") as v_file:
                        st.session_state["final_video_bytes"] = v_file.read()

                    for temp_file in [video_filename, ass_filename, output_filename]:
                        if os.path.exists(temp_file):
                            os.remove(temp_file)

                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    orig_name = uploaded_file.name if uploaded_file else "영상파일"
                    history_entry = f"{now_str} | {orig_name}"

                    st.session_state["history"].insert(0, history_entry)
                    st.session_state["history"] = st.session_state["history"][:10]
                    st.session_state["success_time"] = time.time()

                    st.session_state["processing"] = False
                    st.rerun()

                except Exception as e:
                    st.error(f"영상 합성 중 오류가 발생했습니다: {e}")
                    st.session_state["processing"] = False
