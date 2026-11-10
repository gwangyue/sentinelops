"""所有契约的公共基类。

extra="forbid"：写错字段名直接报错，而不是被悄悄丢掉。旧数据只会比新契约少字段（新字段都带默认值），
所以这条规则不影响"契约升级后旧数据仍能读取"（03 §7）。
"""

from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")
