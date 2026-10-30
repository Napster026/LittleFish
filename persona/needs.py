"""需求状态模型；随时间与活动变化的计算逻辑留待 NeedEngine 实现。"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


NeedValue = Annotated[float, Field(ge=0.0, le=100.0, allow_inf_nan=False)]
NeedDeltaValue = Annotated[float, Field(allow_inf_nan=False)]


# noinspection PyNestedDecorators
class NeedState(BaseModel):
    """四项需求的只读数据模型，创建时必须明确提供所有初始值。

    energy、satiety 越高表示越充足；boredom、social_need 越高表示
    越需要活动或社交来满足。取值范围均为 0～100，越界时拒绝输入，
    不在模型层静默截断。具体变化速率和初始值由后续策略决定。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    energy: NeedValue
    satiety: NeedValue
    boredom: NeedValue
    social_need: NeedValue

    @field_validator("energy", "satiety", "boredom", "social_need", mode="before")
    @classmethod
    def validate_numeric_input(cls, value: Any) -> Any:
        """只接受整数或浮点数，拒绝布尔值和数值字符串。"""
        if type(value) not in (int, float):
            raise ValueError("需求值必须是整数或浮点数，不能是布尔值或字符串")
        return value


# noinspection PyNestedDecorators
class NeedDelta(BaseModel):
    """需求变化量；正数表示增加，负数表示减少，未提供的项为 0。

    至少一项必须非零。这里只检查数值类型与有限性；变化量不是最终状态，
    不沿用 NeedState 的 0～100 范围。单次变化限制与最终状态限幅由后续
    结算策略处理。正负变化的实际含义与 NeedState 四项需求的方向一致。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    energy: NeedDeltaValue = 0.0
    satiety: NeedDeltaValue = 0.0
    boredom: NeedDeltaValue = 0.0
    social_need: NeedDeltaValue = 0.0

    @field_validator("energy", "satiety", "boredom", "social_need", mode="before")
    @classmethod
    def validate_numeric_input(cls, value: Any) -> Any:
        """拒绝把布尔值、数值字符串等隐式转换成变化量。"""
        if type(value) not in (int, float):
            raise ValueError("需求变化量必须是整数或浮点数")
        return value

    @model_validator(mode="after")
    def validate_nonzero_delta(self) -> NeedDelta:
        """检查四项中至少一项有变化，不能按总和判断。"""
        if not any(
            value != 0.0
            for value in (self.energy, self.satiety, self.boredom, self.social_need)
        ):
            raise ValueError("需求变化量至少有一项非零")
        return self
