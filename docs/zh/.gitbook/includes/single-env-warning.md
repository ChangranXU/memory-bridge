{% hint style="warning" %}
始终使用 bundle 根目录的共享环境。切勿在集成目录内运行 `uv`，切勿 `pip install` 到共享环境，也切勿通过 PYTHONPATH hack 导入集成。
{% endhint %}
