"""模型型号的固定版本校验（G2，06 篇 §4.6；08 §6.4"模型不能用别名"）。

负责：判断一个型号字符串是不是别名。加载 config/models.yaml 时调用，D3 的 `sentinel version freeze` 也复用。
不负责：型号是否真实存在（那要访问网络，留给冻结时的人工确认）。
"""

# 别名的特征：会随时间指向不同模型的名字。latest 是最常见的；这里不做白名单，
# 因为每家厂商的版本号格式不同，白名单会挡住合法的新型号。
# TODO(edge): 其他厂商的别名写法（例如不带日期的滚动名）出现时再补规则
_ALIAS_MARKERS = ("latest",)


class ModelAliasError(ValueError):
    pass


def assert_pinned(model: str) -> str:
    """型号是固定版本就原样返回，是别名就抛错。"""
    name = model.strip().lower()
    if not name:
        raise ModelAliasError("型号为空")
    for marker in _ALIAS_MARKERS:
        if marker in name:
            raise ModelAliasError(f"型号 {model!r} 是别名（含 {marker!r}），必须写固定版本（G2）")
    return model
