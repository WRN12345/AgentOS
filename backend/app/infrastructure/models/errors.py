"""模型调用的统一错误封装。

Provider 内部一律把 httpx 的传输/超时/非 2xx 错误转换为这里的自定义异常，
业务与 Agent 代码只面向这两个错误处理，不接触 httpx 异常类型。
"""


class ModelError(Exception):
    """模型调用失败的基类。"""


class ModelUnavailableError(ModelError):
    """模型服务不可用：连接失败、DNS 失败、非 2xx 响应等。"""


class ModelTimeoutError(ModelError):
    """模型调用超时（连接/读取/写入超时）。"""
