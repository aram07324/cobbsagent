from jinja2 import Markup, Template


def render(name):
    return Template("<p>{{ name }}</p>").render(name=Markup("<b>%s</b>") % name)
