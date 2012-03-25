from types import ListType


def add(d, key, value):
    if len(key) == 0:
        if len(d) == 0:
            d = []
        d.append(value)
        #print "%s" % r
    else:
        if key not in d:
            d[key] = value
        else:
            if not type(d[key]) is ListType:
                tmp = d[key]
                d[key] = []
                d[key].append(tmp)
            d[key].append(value)
    return d


def _parse_result_line(line):
    start = 0
    inComment = False
    key = ""
    value = ""
    i = 0
    subparse = 0
    d = {}
    while i < len(line):
        c = line[i]
        if inComment:
            if c == "\"":
                inComment = False
                value = line[start:i].decode("string-escape")
                d = add(d, key, value)
                key = ""
                start = i + 1
            elif c == "\\":
                if line[i + 1] == "\"":
                    i += 1
        else:
            if c == "=":
                key = line[start:i]
                start = i + 1
            elif c == "\"":
                inComment = True
                start = i + 1
            elif c == "," or c == " " or c == "\n" or c == "\r":
                start = i + 1
            elif c == "{" or c == "[":
                subparse += 1
                start = i + 1
                (pos, r) = _parse_result_line(line[start:])
                d = add(d, key, r)
                i = start + pos
                continue
            elif c == "}" or c == "]":
                if subparse > 0:
                    subparse -= 1
                else:
                    break

        i += 1
    return (i, d)


def parse_result_line(line):
    return _parse_result_line(line)[1]
