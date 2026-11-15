from jinja2 import Environment, contextfunction


@contextfunction
def greet(context, name):
    return context.get("prefix", "") + name


def render(name):
    env = Environment()
    env.globals["greet"] = greet
    return env.from_string("{{ greet(name) }}").render(prefix="Hi ", name=name)
