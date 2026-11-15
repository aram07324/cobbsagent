import idna


def to_ascii(host):
    return idna.encode(host).decode("ascii")
