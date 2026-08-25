{% hint style="warning" %}
Always use the shared environment at the bundle root. Never run `uv` inside an integration directory, never `pip install` into the shared env, and never import an integration via a PYTHONPATH hack.
{% endhint %}
