import pytest
import graphics.volumetric_light_shaft as mod

CASES = [
    ("occlusion_depth", (1)),
    ("step_smooth", (1)),
    ("light_pos", (1)),
    ("shadow_fence", (1)),
    ("fog_entry", (1)),
    ("sample_determinism", (1)),
    ("ray_march", (1)),
    ("validation", (1)),
    ("audit", (1)),
    ("fallback", (1)),
    ("transparent_path", (1)),
    ("metrics", (1)),
    ("depth_reconstruct", (1)),
    ("intensity", (1)),
    ("determinism", (1)),
]

@pytest.mark.parametrize("fn_name,args", CASES)
def test_contract(fn_name, args):
    fn = getattr(mod, fn_name)
    assert fn(*args) is not None
