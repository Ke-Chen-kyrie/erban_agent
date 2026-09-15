from __future__ import annotations

import json
from typing import Any

import cv2
import numpy as np


def pretty_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def format_score(value: Any) -> str:
    if value is None or value == "":
        return "无"
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value)


def format_result(data: Any, result_type: str) -> str:
    if result_type == "register":
        return "\n".join([
            "注册成功",
            f"用户 ID：{data.get('user_id', '')}",
            f"姓名：{data.get('name', '')}",
            f"人脸 ID：{data.get('face_id') or '无'}",
        ])

    if result_type == "face_search":
        return _format_ranked_results("人脸搜索结果", data, "entity_id")

    if result_type == "voice_search":
        return _format_ranked_results("声纹搜索结果", data, "feature_id")

    if result_type == "face_verify":
        matched = bool(data.get("match"))
        lines = [
            "人脸验证结果",
            f"结论：{'通过' if matched else '未通过'}",
            f"匹配分数：{format_score(data.get('score'))}",
            f"置信度：{format_score(data.get('confidence'))}",
            f"阈值：{format_score(data.get('threshold'))}",
        ]
        if data.get("request_id"):
            lines.append(f"请求 ID：{data['request_id']}")
        return "\n".join(lines)

    if result_type == "face_detect":
        faces = data.get("faces") or []
        lines = [
            "人脸检测结果",
            f"检测到 {data.get('total_faces', 0)} 张人脸（已识别 {data.get('matched_count', 0)}，未知 {data.get('unknown_count', 0)}）",
        ]
        for index, face in enumerate(faces, start=1):
            loc = face.get("location") or {}
            status = "已识别" if face.get("matched") else "未知"
            lines.extend([
                "",
                f"#{index} {face.get('name') or face.get('user_id') or '未知'}  [{status}]",
                f"用户 ID：{face.get('user_id') or '—'}",
                f"匹配分数：{format_score(face.get('score'))}",
                f"位置：({loc.get('x')},{loc.get('y')}) {loc.get('width')}x{loc.get('height')}",
            ])
            if face.get("mouth"):
                lines.append(f"嘴部关键点：{len(face['mouth'])} 个")
        if data.get("request_id"):
            lines.extend(["", f"请求 ID：{data['request_id']}"])
        return "\n".join(lines)

    if result_type == "sync":
        voice_cleanup = data.get("voice_cleanup") or []
        face_cleanup = data.get("face_cleanup") or []
        return "\n".join([
            "同步清理完成",
            f"声纹清理：{', '.join(map(str, voice_cleanup)) if voice_cleanup else '无'}",
            f"人脸清理：{', '.join(map(str, face_cleanup)) if face_cleanup else '无'}",
        ])

    if result_type == "delete":
        return str(data.get("message") or "删除完成")

    return pretty_json(data)


def annotate_face_detect(jpg: bytes, data: dict[str, Any], match_threshold: float = 0.5) -> np.ndarray | None:
    image = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None
    for face in data.get("faces", []):
        loc = face.get("location")
        if not loc:
            continue
        x, y, w, h = loc["x"], loc["y"], loc["width"], loc["height"]
        try:
            score = float(face.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        is_match = bool(face.get("matched")) and score >= match_threshold
        color = (0, 180, 75) if is_match else (0, 80, 230)
        cv2.rectangle(image, (x, y), (x + w, y + h), color, 2)
        label = str(face.get("user_id") or "unknown")
        if face.get("name"):
            label = f"{face['name']}({face.get('user_id')})"
        label = f"{label} score={format_score(face.get('score'))}"
        label_origin = (x, max(18, y - 10))
        text_size, baseline = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 2)
        bg_x2 = min(image.shape[1] - 1, x + text_size[0] + 6)
        bg_y1 = max(0, label_origin[1] - text_size[1] - 5)
        bg_y2 = min(image.shape[0] - 1, label_origin[1] + baseline + 3)
        cv2.rectangle(image, (x, bg_y1), (bg_x2, bg_y2), (255, 255, 255), -1)
        cv2.putText(image, label, label_origin, cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
        for point in face.get("mouth", []):
            cv2.circle(image, (int(point["x"]), int(point["y"])), 2, (230, 120, 0), -1)
    return image


def _format_ranked_results(title: str, data: Any, fallback_id_key: str) -> str:
    results = data.get("results") or []
    if not results:
        return f"{title.replace('结果', '')}未找到匹配结果。"
    lines = [title]
    for index, item in enumerate(results, start=1):
        user_id = item.get("user_id") or item.get(fallback_id_key) or "未知"
        name = item.get("name") or user_id
        lines.extend([
            "",
            f"#{index} {name}",
            f"用户 ID：{user_id}",
            f"匹配分数：{format_score(item.get('score'))}",
        ])
        if "confidence" in item:
            lines.append(f"置信度：{format_score(item.get('confidence'))}")
        if "feature_info" in item:
            lines.append(f"特征信息：{item.get('feature_info') or '无'}")
    if data.get("request_id"):
        lines.extend(["", f"请求 ID：{data['request_id']}"])
    return "\n".join(lines)
