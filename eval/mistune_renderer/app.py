import mistune


class TitleRenderer(mistune.Renderer):
    def header(self, text, level, raw=None):
        return '<h%d class="title">%s</h%d>\n' % (level, text, level)


def to_html(md_text):
    return mistune.Markdown(renderer=TitleRenderer())(md_text)
