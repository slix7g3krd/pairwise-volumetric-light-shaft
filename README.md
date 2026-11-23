# pairwise-volumetric-light-shaft

## 领域

游戏、图形与多媒体

## 主题

体积光轴遮挡

## 说明

透明遮挡深度、步长平滑与采样确定。

初始状态下 `graphics/volumetric_light_shaft.py` 中所有函数抛出 `NotImplementedError`，测试全部失败。
修复实现后运行：

```bash
python -m pytest -q
```

期望全部通过。
