"""供测试上位机展示的用例说明。"""
from inspect import cleandoc


def execution_logic(text: str):
    """附加执行逻辑，保留用例函数及其显示名。"""
    def decorate(function):
        function._bench_execution_logic = cleandoc(text)
        return function

    return decorate
