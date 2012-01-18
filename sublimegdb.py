"""
Copyright (c) 2012 Fredrik Ehnbom

This software is provided 'as-is', without any express or implied
warranty. In no event will the authors be held liable for any damages
arising from the use of this software.

Permission is granted to anyone to use this software for any purpose,
including commercial applications, and to alter it and redistribute it
freely, subject to the following restrictions:

   1. The origin of this software must not be misrepresented; you must not
   claim that you wrote the original software. If you use this software
   in a product, an acknowledgment in the product documentation would be
   appreciated but is not required.

   2. Altered source versions must be plainly marked as such, and must not be
   misrepresented as being the original software.

   3. This notice may not be removed or altered from any source
   distribution.
"""
import sublime
import sublime_plugin
import subprocess
import threading
import time
import traceback
import os
import re
import Queue

DEBUG = True

breakpoints = {}
gdb_lastresult = ""
gdb_lastline = ""
gdb_cursor = ""
gdb_cursor_position = 0

gdb_process = None
gdb_locals = []


gdb_session_view = None
gdb_console_view = None
gdb_locals_view = None
gdb_callstack_view = None
result_regex = re.compile("(?<=\^)[^,]*")


def log_debug(line):
    if DEBUG:
        os.system("echo \"%s\" >> /tmp/debug.txt" % line)


class GDBView:
    LINE = 0
    FOLD_ALL = 1
    CLEAR = 2
    SCROLL = 3
    VIEWPORT_POSITION = 4

    def __init__(self, name, scroll=True):
        self.queue = Queue.Queue()
        self.name = name
        self.closed = False
        self.create_view()
        self.scroll = scroll

    def add_line(self, line):
        self.queue.put((GDBView.LINE, line))
        sublime.set_timeout(self.update, 0)

    def scroll(self, line):
        self.queue.put((GDBView.SCROLL, line))

    def set_viewport_position(self, pos):
        self.queue.put((GDBView.VIEWPORT_POSITION, pos))

    def clear(self):
        self.queue.put((GDBView.CLEAR, None))

    def create_view(self):
        self.view = sublime.active_window().new_file()
        self.view.set_name(self.name)
        self.view.set_scratch(True)
        self.view.set_read_only(True)

    def is_closed(self):
        return self.closed

    def was_closed(self):
        self.closed = True

    def fold_all(self):
        self.queue.put((GDBView.FOLD_ALL, None))

    def get_view(self):
        return self.view

    def update(self):
        e = self.view.begin_edit()
        self.view.set_read_only(False)
        try:
            while True:
                cmd, data = self.queue.get_nowait()
                if cmd == GDBView.LINE:
                    self.view.insert(e, self.view.size(), data)
                elif cmd == GDBView.FOLD_ALL:
                    self.view.run_command("fold_all")
                elif cmd == GDBView.CLEAR:
                    self.view.erase(e, sublime.Region(0, self.view.size()))
                elif cmd == GDBView.SCROLL:
                    self.view.show(self.view.text_point(data, 0))
                elif cmd == GDBView.VIEWPORT_POSITION:
                    self.view.set_viewport_position(data)

                self.queue.task_done()
        except:
            # get_nowait throws an exception when there's nothing..
            pass
        finally:
            self.view.end_edit(e)
            self.view.set_read_only(True)
            if self.scroll:
                self.view.show(self.view.size())


class GDBValuePairs:
    def __init__(self, string):
        string = string.split("\",")
        self.data = {}
        for pair in string:
            if not "=" in pair:
                continue
            key, value = pair.split("=", 1)
            value = value.replace("\"", "")
            self.data[key] = value

    def __getitem__(self, key):
        return self.data[key]

    def __str__(self):
        return "%s" % self.data


class GDBVariable:
    def __init__(self, vp):
        self.valuepair = vp
        self.children = []
        self.line = 0
        self.is_expanded = False

    def expand(self):
        self.is_expanded = True
        if not (len(self.children) == 0 and int(self.valuepair["numchild"]) > 0):
            return
        line = run_cmd("-var-list-children 1 \"%s\"" % self.valuepair["name"], True)
        children = re.split("[},|{]child=\{", line[:line.rfind("}}") + 1])[1:]
        for child in children:
            child = GDBValuePairs(child[:-1])
            self.children.append(GDBVariable(child))

    def has_children(self):
        return int(self.valuepair["numchild"]) > 0

    def collapse(self):
        self.is_expanded = False

    def __str__(self):
        return "%s %s = %s" % (self.valuepair['type'], self.valuepair['exp'], self.valuepair['value'])

    def format(self, indent="", output="", line=0):
        icon = " "
        if self.has_children():
            if self.is_expanded:
                icon = "-"
            else:
                icon = "+"
        output += "%s%s%s\n" % (indent, icon, self)
        self.line = line
        line = line + 1
        indent += "\t"
        if self.is_expanded:
            for child in self.children:
                output, line = child.format(indent, output, line)
        return (output, line)


def variable_stuff(line, indent=""):
    line = line[line.find("value=") + 7:]
    if line[0] == "{":
        line = line[1:]
    start = 0
    level = 0
    output = ""
    for idx in range(len(line)):
        char = line[idx]
        if char == '{':
            data = line[start:idx].strip()
            output += "%s%s\n" % (indent, data)

            start = idx + 1
            indent = indent + "\t"
        elif char == '}':
            output += "%s%s" % (indent, line[start:idx].strip())
            start = idx + 1
            indent = indent[:-1]
        elif char == "," and level == 0:
            data = line[start:idx].strip()
            output += "%s%s\n" % (indent, data)
            start = idx + 1
        elif char == "\"":
            data = line[start:idx].strip()
            output += "%s%s\n" % (indent, data)
            break
        elif char == "(" or char == "<":
            level += 1
        elif char == ")" or char == ">":
            level -= 1

    return output


def extract_varobjs(line):
    varobjs = line[:line.rfind("}}") + 1]
    varobjs = varobjs.split("varobj=")[1:]
    ret = []
    for varobj in varobjs:
        var = GDBValuePairs(varobj[1:-1])
        ret.append(var)
    return ret


def update_locals_view():
    gdb_locals_view.clear()
    output = ""
    line = 0
    for local in gdb_locals:
        output, line = local.format(line=line)
        gdb_locals_view.add_line(output)


def get_variable_at_line(line, var_list):
    if len(var_list) == 0:
        return None

    for i in range(len(var_list)):
        if var_list[i].line == line:
            return var_list[i]
        elif var_list[i].line > line:
            return get_variable_at_line(line, var_list[i - 1].children)
    return get_variable_at_line(line, var_list[len(var_list) - 1].children)


def locals(line):
    global gdb_locals
    gdb_locals = []
    loc = extract_varobjs(line)
    for var in loc:
        gdb_locals.append(GDBVariable(var))
    update_locals_view()


def extract_breakpoints(line):
    gdb_breakpoints = []
    bps = re.findall("(?<=,bkpt\=\{)[^}]+", line)
    for bp in bps:
        gdb_breakpoints.append(GDBValuePairs(bp))
    return gdb_breakpoints


def extract_stackframes(line):
    gdb_stackframes = []
    frames = re.findall("(?<=frame\=\{)[^}]+", line)
    for frame in frames:
        gdb_stackframes.append(GDBValuePairs(frame))
    return gdb_stackframes


def extract_stackargs(line):
    gdb_stackargs = []
    frames = line.split("level=")[1:]
    for frame in frames:
        curr = []
        args = re.findall("name=\"[^\"]+\",value=\"[^\"]+\"", frame)
        for arg in args:
            curr.append(GDBValuePairs(arg))
        gdb_stackargs.append(curr)
    return gdb_stackargs


def update(view=None):
    if view == None:
        view = sublime.active_window().active_view()
    bps = []
    fn = view.file_name()
    if fn in breakpoints:
        for line in breakpoints[fn]:
            if not (line == gdb_cursor_position and fn == gdb_cursor):
                bps.append(view.full_line(view.text_point(line - 1, 0)))
    view.add_regions("sublimegdb.breakpoints", bps, "keyword.gdb", "circle", sublime.HIDDEN)
    cursor = []

    if fn == gdb_cursor and gdb_cursor_position != 0:
        cursor.append(view.full_line(view.text_point(gdb_cursor_position - 1, 0)))

    view.add_regions("sublimegdb.position", cursor, "entity.name.class", "bookmark", sublime.HIDDEN)

count = 0


def run_cmd(cmd, block=False, mimode=True):
    global count
    if mimode:
        count = count + 1
        cmd = "%d%s\n" % (count, cmd)
    else:
        cmd = "%s\n\n" % cmd
    log_debug(cmd)
    gdb_session_view.add_line(cmd)
    gdb_process.stdin.write(cmd)
    if block:
        countstr = "%d^" % count
        while not gdb_lastresult.startswith(countstr):
            time.sleep(0.1)
        return gdb_lastresult
    return count


def wait_until_stopped():
    result = run_cmd("-exec-interrupt", True)
    if "^done" in result:
        while not "stopped" in gdb_lastline:
            time.sleep(0.1)
        return True
    return False


def resume():
    run_cmd("-exec-continue")


def add_breakpoint(filename, line):
    breakpoints[filename].append(line)
    if is_running():
        res = wait_until_stopped()
        run_cmd("-break-insert %s:%d" % (filename, line))
        if res:
            resume()


def remove_breakpoint(filename, line):
    breakpoints[filename].remove(line)
    if is_running():
        res = wait_until_stopped()
        gdb_breakpoints = extract_breakpoints(run_cmd("-break-list", True))
        for bp in gdb_breakpoints:
            if bp.data["file"] == filename and bp.data["line"] == str(line):
                run_cmd("-break-delete %s" % bp.data["number"])
                break
        if res:
            resume()


def toggle_breakpoint(filename, line):
    if line in breakpoints[filename]:
        remove_breakpoint(filename, line)
    else:
        add_breakpoint(filename, line)


def sync_breakpoints():
    global breakpoints
    newbps = {}
    for file in breakpoints:
        for bp in breakpoints[file]:
            if file in newbps:
                if bp in newbps[file]:
                    continue
            cmd = "-break-insert %s:%d" % (file, bp)
            out = run_cmd(cmd, True)
            if get_result(out) == "error":
                continue
            bp = extract_breakpoints(out)[0]
            f = bp["file"]
            if not f in newbps:
                newbps[f] = []
            newbps[f].append(int(bp["line"]))
    breakpoints = newbps
    update()


def get_result(line):
    return result_regex.search(line).group(0)


def update_cursor():
    global gdb_cursor
    global gdb_cursor_position
    line = run_cmd("-stack-list-frames", True)
    if get_result(line) == "error":
        gdb_cursor_position = 0
        update()
        return
    frames = extract_stackframes(line)
    gdb_cursor = frames[0]["fullname"]
    gdb_cursor_position = int(frames[0]["line"])

    line = run_cmd("-stack-list-arguments 1", True)
    args = extract_stackargs(line)
    gdb_callstack_view.clear()
    for i in range(len(frames)):
        output = "%s(" % frames[i]["func"]
        for v in args[i]:
            output += "%s = %s, " % (v["name"], v["value"])
        output += ")\n"

        gdb_callstack_view.add_line(output)

    sublime.active_window().open_file("%s:%d" % (gdb_cursor, gdb_cursor_position), sublime.ENCODED_POSITION)
    update()
    locals(run_cmd("-stack-list-locals 2", True))


def gdboutput(pipe):
    global gdb_process
    global gdb_lastresult
    global gdb_lastline
    command_result_regex = re.compile("^\d+\^")
    stopped_regex = re.compile("^\d*\*stopped")
    while True:
        try:
            if gdb_process.poll() != None:
                break
            line = pipe.readline().strip()

            if len(line) > 0:
                log_debug(line)
                gdb_session_view.add_line("%s\n" % line)

                if stopped_regex.match(line) != None:
                    sublime.set_timeout(update_cursor, 0)
                if not line.startswith("(gdb)"):
                    gdb_lastline = line
                if "BreakpointTable" in line:
                    extract_breakpoints(line)
                if command_result_regex.match(line) != None:
                    gdb_lastresult = line

                if line.startswith("~"):
                    gdb_console_view.add_line(
                        line[2:-1].replace("\\n", "\n").replace("\\\"", "\"").replace("\\t", "\t"))

        except:
            traceback.print_exc()
    if pipe == gdb_process.stdout:
        gdb_session_view.add_line("GDB session ended\n")
    global gdb_cursor_position
    gdb_cursor_position = 0
    sublime.set_timeout(update, 0)


def show_input():
    sublime.active_window().show_input_panel("GDB", "", input_on_done, input_on_change, input_on_cancel)


def input_on_done(s):
    run_cmd(s)
    if s.strip() != "quit":
        show_input()


def input_on_cancel():
    pass


def input_on_change(s):
    pass


def get_setting(key, default=None):
    try:
        s = sublime.active_window().active_view().settings()
        if s.has("sublimegdb_%s" % key):
            return s.get("sublimegdb_%s" % key)
    except:
        pass
    return sublime.load_settings("SublimeGDB.sublime-settings").get(key, default)


def is_running():
    return gdb_process != None and gdb_process.poll() == None


class GdbInput(sublime_plugin.TextCommand):
    def run(self, edit):
        show_input()


class GdbLaunch(sublime_plugin.TextCommand):
    def run(self, edit):
        global gdb_process
        global gdb_session_view
        global gdb_console_view
        global gdb_locals_view
        global gdb_callstack_view
        if gdb_process == None or gdb_process.poll() != None:
            os.chdir(get_setting("workingdir", "/tmp"))
            commandline = get_setting("commandline")
            gdb_process = subprocess.Popen(commandline, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE)

            w = self.view.window()
            w.set_layout(
                get_setting("layout",
                    {
                        "cols": [0.0, 0.5, 1.0],
                        "rows": [0.0, 0.75, 1.0],
                        "cells": [[0, 0, 2, 1], [0, 1, 1, 2], [1, 1, 2, 2]]
                    }
                )
            )

            if gdb_session_view == None or gdb_session_view.is_closed():
                gdb_session_view = GDBView("GDB Session")
            if gdb_console_view == None or gdb_console_view.is_closed():
                gdb_console_view = GDBView("GDB Console")
            if gdb_locals_view == None or gdb_locals_view.is_closed():
                gdb_locals_view = GDBView("GDB Locals", False)
            if gdb_callstack_view == None or gdb_callstack_view.is_closed():
                gdb_callstack_view = GDBView("GDB Callstack")

            gdb_session_view.clear()
            gdb_console_view.clear()
            gdb_locals_view.clear()
            gdb_callstack_view.clear()

            w.set_view_index(gdb_session_view.get_view(), get_setting("session_group", 1), get_setting("session_index", 0))
            w.set_view_index(gdb_console_view.get_view(), get_setting("console_group", 1), get_setting("console_index", 1))
            w.set_view_index(gdb_locals_view.get_view(), get_setting("locals_group", 2), get_setting("locals_index", 0))
            w.set_view_index(gdb_callstack_view.get_view(), get_setting("callstack_group", 2), get_setting("callstack_index", 0))
            t = threading.Thread(target=gdboutput, args=(gdb_process.stdout,))
            t.start()

            sync_breakpoints()
            run_cmd(get_setting("exec_cmd"), "-exec-run")
            show_input()
        else:
            sublime.status_message("GDB is already running!")

    def is_enabled(self):
        return not is_running()


class GdbContinue(sublime_plugin.TextCommand):
    def run(self, edit):
        global gdb_cursor_position
        gdb_cursor_position = 0
        update(self.view)
        run_cmd("-exec-continue")

    def is_enabled(self):
        return is_running()


class GdbExit(sublime_plugin.TextCommand):
    def run(self, edit):
        run_cmd("-gdb-exit")

    def is_enabled(self):
        return is_running()


class GdbPause(sublime_plugin.TextCommand):
    def run(self, edit):
        run_cmd("-gdb-interrupt")

    def is_enabled(self):
        return is_running()


class GdbStepOver(sublime_plugin.TextCommand):
    def run(self, edit):
        run_cmd("-exec-next")

    def is_enabled(self):
        return is_running()


class GdbStepInto(sublime_plugin.TextCommand):
    def run(self, edit):
        run_cmd("-exec-step")

    def is_enabled(self):
        return is_running()


class GdbNextInstruction(sublime_plugin.TextCommand):
    def run(self, edit):
        run_cmd("-exec-next-instruction")

    def is_enabled(self):
        return is_running()


class GdbStepOut(sublime_plugin.TextCommand):
    def run(self, edit):
        run_cmd("-exec-finish")

    def is_enabled(self):
        return is_running()


class GdbToggleBreakpoint(sublime_plugin.TextCommand):
    def run(self, edit):
        fn = self.view.file_name()
        if fn not in breakpoints:
            breakpoints[fn] = []

        line, col = self.view.rowcol(self.view.sel()[0].a)
        toggle_breakpoint(fn, line + 1)
        update(self.view)


class GdbExpandCollapseVariable(sublime_plugin.TextCommand):
    def run(self, edit):
        if gdb_locals_view != None and self.view.id() == gdb_locals_view.get_view().id():
            row, col = self.view.rowcol(self.view.sel()[0].a)
            var = get_variable_at_line(row, gdb_locals)
            if var and var.has_children():
                if var.is_expanded:
                    var.collapse()
                else:
                    var.expand()
                pos = self.view.viewport_position()
                update_locals_view()
                gdb_locals_view.set_viewport_position(pos)
                gdb_locals_view.update()


class GdbEventListener(sublime_plugin.EventListener):
    def on_query_context(self, view, key, operator, operand, match_all):
        if key == "gdb_running":
            return is_running() == operand
        return None

    def on_activated(self, view):
        if view.file_name() != None:
            update(view)

    def on_load(self, view):
        if view.file_name() != None:
            update(view)

    def on_close(self, view):
        if gdb_session_view != None and view.id() == gdb_session_view.get_view().id():
            gdb_session_view.was_closed()
        if gdb_console_view != None and view.id() == gdb_console_view.get_view().id():
            gdb_console_view.was_closed()
        if gdb_locals_view != None and view.id() == gdb_locals_view.get_view().id():
            gdb_locals_view.was_closed()
        if gdb_callstack_view != None and view.id() == gdb_callstack_view.get_view().id():
            gdb_callstack_view.was_closed()
