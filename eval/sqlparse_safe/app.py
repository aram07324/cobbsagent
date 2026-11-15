import sqlparse


def pretty(sql):
    return sqlparse.format(sql, reindent=True, keyword_case="upper")
