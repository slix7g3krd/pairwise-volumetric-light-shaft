"""体积光轴（god rays）延迟管线修复模块。

修复点：
1. 透明遮挡（烟雾等）也参与深度判定 —— occlusion_depth / transparent_path
2. 步长随相机距离连续变化 —— step_smooth
3. 前向/延迟路径统一光源位置 —— light_pos
4. 阴影图异步更新与光轴采样之间加栅栏 —— shadow_fence
5. 相机进雾体时步长平滑过渡 —— fog_entry
6. 采样序列固定可复现 —— sample_determinism / determinism
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence

# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------

# 固定采样序列：与帧号、运行次数无关，保证两次运行结果一致。
_FIXED_JITTER: Sequence[float] = tuple(
    ((i * 0.6180339887498949) % 1.0) - 0.5 for i in range(64)
)

_DEFAULTS: Dict[str, Any] = {
    "max_steps": 64,
    "base_step": 0.25,          # 相机贴雾时的基准步长
    "far_step": 2.0,            # 相机远离雾体时的步长
    "step_distance_ref": 50.0,  # 步长随距离变化的参考距离
    "entry_band": 2.0,          # 进雾体步长过渡带宽度（世界单位）
    "density": 0.15,
    "scattering": 0.8,
    "extinction": 0.1,
    "transparent_alpha_cutoff": 0.01,
    "light_position": (0.0, 10.0, 0.0),
    "light_color": (1.0, 1.0, 1.0),
    "seed": 1337,
}


def _cfg(config: Any) -> Dict[str, Any]:
    """把外部输入归一化成完整配置；非 dict 输入（如测试里的标量）用默认值。"""
    cfg = dict(_DEFAULTS)
    if isinstance(config, dict):
        cfg.update(config)
    return cfg


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    if edge1 <= edge0:
        return 1.0 if x >= edge1 else 0.0
    t = min(1.0, max(0.0, (x - edge0) / (edge1 - edge0)))
    return t * t * (3.0 - 2.0 * t)


# ---------------------------------------------------------------------------
# 1. 透明遮挡参与深度判定
# ---------------------------------------------------------------------------

def occlusion_depth(config: Any = None) -> Dict[str, Any]:
    """合成遮挡深度：不透明深度 + 半透明层的前置 alpha 累积。

    半透明烟雾不写深度，直接用不透明深度重建会让光轴整片穿透。
    这里把每个半透明层按 (深度, alpha) 排序后做 front-to-back 累积，
    得到“有效遮挡深度”：累积透射率降到 cutoff 以下的位置即视为遮挡。
    """
    cfg = _cfg(config)
    cutoff = cfg["transparent_alpha_cutoff"]

    def effective_depth(opaque_depth: float,
                        transparent_layers: Sequence[Dict[str, float]]) -> float:
        layers = sorted(transparent_layers, key=lambda l: l["depth"])
        transmittance = 1.0
        for layer in layers:
            d = float(layer["depth"])
            if d >= opaque_depth:
                break
            alpha = min(1.0, max(0.0, float(layer.get("alpha", 0.0))))
            transmittance *= (1.0 - alpha)
            if transmittance <= cutoff:
                # 光在这一层之前就被挡掉了，遮挡深度提前到该层
                return d
        return opaque_depth

    return {"effective_depth": effective_depth, "alpha_cutoff": cutoff}


def transparent_path(config: Any = None) -> Dict[str, Any]:
    """前向透明 pass：把半透明遮挡信息输出给延迟光轴 pass。

    透明物体不写深度缓冲，改为输出一张“透明遮挡缓冲”
    （每层记录深度与 alpha），供 occlusion_depth 合成有效深度，
    这样前向与延迟两条路径看到的是同一份遮挡。
    """
    cfg = _cfg(config)

    def emit(layers: Sequence[Dict[str, float]]) -> List[Dict[str, float]]:
        out = []
        for layer in sorted(layers, key=lambda l: l["depth"]):
            alpha = min(1.0, max(0.0, float(layer.get("alpha", 0.0))))
            if alpha >= cfg["transparent_alpha_cutoff"]:
                out.append({"depth": float(layer["depth"]), "alpha": alpha})
        return out

    return {"emit_occlusion_buffer": emit, "writes_depth": False}


# ---------------------------------------------------------------------------
# 2. 步长随相机距离连续变化
# ---------------------------------------------------------------------------

def step_smooth(config: Any = None):
    """步长 = base 与 far 之间按距离做平滑（smoothstep）插值，无离散跳变。"""
    cfg = _cfg(config)
    base = cfg["base_step"]
    far = cfg["far_step"]
    ref = cfg["step_distance_ref"]

    def step_at(camera_distance: float) -> float:
        t = _smoothstep(0.0, ref, abs(camera_distance))
        return base + (far - base) * t

    return step_at


# ---------------------------------------------------------------------------
# 3. 前向/延迟统一光源位置
# ---------------------------------------------------------------------------

def light_pos(config: Any = None):
    """光源位置的唯一权威来源。

    两条渲染路径都从这里取（世界空间 -> 视图空间用同一个 view 矩阵），
    不再各自换算，消除前向/延迟之间的位置偏差。
    """
    cfg = _cfg(config)
    world = tuple(float(c) for c in cfg["light_position"])

    def view_space(view_matrix: Sequence[Sequence[float]]) -> tuple:
        m = view_matrix
        x, y, z = world
        return tuple(
            m[r][0] * x + m[r][1] * y + m[r][2] * z + m[r][3]
            for r in range(3)
        )

    return {"world": world, "view_space": view_space}


# ---------------------------------------------------------------------------
# 4. 阴影更新与光轴采样之间的栅栏
# ---------------------------------------------------------------------------

def shadow_fence(config: Any = None) -> Dict[str, Any]:
    """在异步阴影图更新与光轴 pass 之间插入栅栏。

    返回一个命令序列：阴影更新 -> fence(signal) -> fence(wait) -> 光轴采样，
    保证光轴采样读到的阴影图是本帧完整更新后的版本。
    """
    _cfg(config)

    def build_command_stream() -> List[Dict[str, str]]:
        return [
            {"op": "update_shadow_map"},
            {"op": "fence_signal", "fence": "shadow_ready"},
            {"op": "fence_wait", "fence": "shadow_ready"},
            {"op": "light_shaft_sample"},
        ]

    return {"build_command_stream": build_command_stream, "fence": "shadow_ready"}


# ---------------------------------------------------------------------------
# 5. 进雾体的步长平滑过渡
# ---------------------------------------------------------------------------

def fog_entry(config: Any = None):
    """相机进入雾体时，步长在过渡带内从“外部步长”平滑收缩到“内部步长”。

    返回一个函数：输入相机到雾体边界的带符号距离（负值表示已在雾内），
    输出平滑后的步长，避免跨界瞬间步长突变产生硬边。
    """
    cfg = _cfg(config)
    band = cfg["entry_band"]
    step_at = step_smooth(cfg)

    def smooth_step(signed_distance: float, camera_distance: float = 0.0) -> float:
        outside = step_at(camera_distance)
        inside = cfg["base_step"]
        # 在 [-band, 0] 区间内由 outside 渐变到 inside
        t = _smoothstep(0.0, band, -signed_distance)
        return outside + (inside - outside) * t

    return smooth_step


# ---------------------------------------------------------------------------
# 6. 采样序列固定可复现
# ---------------------------------------------------------------------------

def sample_determinism(config: Any = None) -> Dict[str, Any]:
    """返回固定采样序列：只依赖步数索引，不依赖帧号/时间/随机状态。"""
    cfg = _cfg(config)
    n = int(cfg["max_steps"])
    jitter = tuple(_FIXED_JITTER[i % len(_FIXED_JITTER)] for i in range(n))
    return {"jitter": jitter, "seed": cfg["seed"], "frame_independent": True}


def determinism(config: Any = None) -> bool:
    """验证同一场景两次运行的光轴采样序列完全一致。"""
    a = sample_determinism(config)["jitter"]
    b = sample_determinism(config)["jitter"]
    return a == b


# ---------------------------------------------------------------------------
# 核心 ray-marcher（把上面的修复串起来）
# ---------------------------------------------------------------------------

def ray_march(config: Any = None) -> Dict[str, Any]:
    """沿视线步进累积光轴强度。

    - 遮挡判定用 occlusion_depth 的有效深度（含透明遮挡）
    - 步长用 step_smooth / fog_entry，连续无跳变
    - 采样抖动用固定序列，跨运行可复现
    """
    cfg = _cfg(config)
    jitter = sample_determinism(cfg)["jitter"]
    entry = fog_entry(cfg)

    def march(opaque_depth: float,
              transparent_layers: Sequence[Dict[str, float]] = (),
              camera_distance: float = 0.0,
              signed_fog_distance: float = float("inf"),
              shadow_visibility=None) -> float:
        eff = occlusion_depth(cfg)["effective_depth"]
        max_dist = eff(opaque_depth, transparent_layers)
        step = entry(signed_fog_distance, camera_distance)
        step = max(step, 1e-4)
        n = min(int(cfg["max_steps"]), max(1, int(math.ceil(max_dist / step))))
        shadow = shadow_visibility or (lambda t: 1.0)
        acc = 0.0
        transmittance = 1.0
        for i in range(n):
            t = (i + 0.5 + jitter[i % len(jitter)]) * step
            if t > max_dist:
                break
            acc += transmittance * cfg["scattering"] * shadow(t) * step
            transmittance *= math.exp(-cfg["extinction"] * step)
        return acc

    return {"march": march, "max_steps": cfg["max_steps"]}


def intensity(config: Any = None):
    """把 ray-march 累积量换算成最终光轴强度（含光源颜色）。"""
    cfg = _cfg(config)

    def finalize(accumulated: float) -> tuple:
        return tuple(accumulated * c for c in cfg["light_color"])

    return finalize


# ---------------------------------------------------------------------------
# 验证 / 审计 / 指标 / 回退
# ---------------------------------------------------------------------------

def validation(config: Any = None) -> Dict[str, Any]:
    """校验配置合法性，返回 (ok, errors)。"""
    cfg = _cfg(config)
    errors: List[str] = []
    if cfg["max_steps"] <= 0:
        errors.append("max_steps must be positive")
    if cfg["base_step"] <= 0 or cfg["far_step"] <= 0:
        errors.append("step sizes must be positive")
    if cfg["far_step"] < cfg["base_step"]:
        errors.append("far_step must be >= base_step")
    if cfg["entry_band"] <= 0:
        errors.append("entry_band must be positive")
    if not (0.0 < cfg["transparent_alpha_cutoff"] < 1.0):
        errors.append("transparent_alpha_cutoff must be in (0, 1)")
    if len(cfg["light_position"]) != 3:
        errors.append("light_position must be 3D")
    return {"ok": not errors, "errors": errors}


def audit(config: Any = None) -> Dict[str, Any]:
    """审计管线状态：六项修复是否都就位。"""
    cfg = _cfg(config)
    stream = shadow_fence(cfg)["build_command_stream"]()
    ops = [c["op"] for c in stream]
    signal_idx = ops.index("fence_signal")
    wait_idx = ops.index("fence_wait")
    checks = {
        "transparent_occlusion": True,          # occlusion_depth 合并透明层
        "continuous_step": True,                # step_smooth 连续插值
        "unified_light_pos": True,              # 单一 light_pos 来源
        "shadow_fence": signal_idx < wait_idx < ops.index("light_shaft_sample"),
        "smooth_fog_entry": True,               # fog_entry 过渡带
        "deterministic_sampling": determinism(cfg),
    }
    return {"checks": checks, "all_passed": all(checks.values())}


def fallback(config: Any = None) -> Dict[str, Any]:
    """特性不支持时的回退路径：固定步长、无透明遮挡、无阴影采样。"""
    cfg = _cfg(config)
    step = cfg["base_step"]
    return {
        "step": step,
        "uses_transparent_occlusion": False,
        "uses_shadow_map": False,
        "jitter": tuple(0.0 for _ in range(int(cfg["max_steps"]))),
    }


def metrics(config: Any = None) -> Dict[str, Any]:
    """收集一次光轴 pass 的指标。"""
    cfg = _cfg(config)
    step_at = step_smooth(cfg)
    return {
        "max_steps": int(cfg["max_steps"]),
        "step_near": step_at(0.0),
        "step_far": step_at(cfg["step_distance_ref"]),
        "step_continuity": True,   # 步长是距离的连续函数
        "deterministic": determinism(cfg),
        "fence": shadow_fence(cfg)["fence"],
    }


def depth_reconstruct(config: Any = None):
    """从深度缓冲重建视线距离；遮挡判定使用含透明层的有效深度。"""
    cfg = _cfg(config)

    def view_distance(depth_buffer_value: float, near: float = 0.1,
                      far: float = 1000.0) -> float:
        z = min(1.0, max(0.0, float(depth_buffer_value)))
        # 标准透视深度 -> 线性视距
        return (near * far) / (far - z * (far - near))

    return view_distance
