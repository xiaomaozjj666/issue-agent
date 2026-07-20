"""领域异常类型：所有业务层抛出的运行时错误集中定义。

放在独立模块，避免 agent.py ↔ reviewer.py ↔ report_generator.py 之间的循环导入。
每个异常名要能自解释，让调用方一眼看出错误来源。
"""


class ModelResponseError(RuntimeError):
    """模型返回不可用：空响应、无 choices、JSON 校验失败、报告无效等。"""


class ReviewResponseError(RuntimeError):
    """独立审查器返回不可用：空响应、无 choices、JSON 校验失败等。"""
