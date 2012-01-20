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
gdb_variables = []
gdb_stack_frame = None
gdb_stack_frames = []
gdb_stack_index = 0


gdb_session_view = None
gdb_console_view = None
gdb_variables_view = None
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

    def __init__(self, name, s=True):
        self.queue = Queue.Queue()
        self.name = name
        self.closed = False
        self.create_view()
        self.doScroll = s

    def add_line(self, line):
        self.queue.put((GDBView.LINE, line))
        sublime.set_timeout(self.update, 0)

    def scroll(self, line):
        self.queue.put((GDBView.SCROLL, line))
        sublime.set_timeout(self.update, 0)

    def set_viewport_position(self, pos):
        self.queue.put((GDBView.VIEWPORT_POSITION, pos))
        sublime.set_timeout(self.update, 0)

    def clear(self):
        self.queue.put((GDBView.CLEAR, None))
        sublime.set_timeout(self.update, 0)

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

        self.view.set_read_only(False)
        try:
            while True:
                cmd, data = self.queue.get_nowait()
                if cmd == GDBView.LINE:
                    e = self.view.begin_edit()
                    self.view.insert(e, self.view.size(), data)
                    self.view.end_edit(e)
                elif cmd == GDBView.FOLD_ALL:
                    self.view.run_command("fold_all")
                elif cmd == GDBView.CLEAR:
                    e = self.view.begin_edit()
                    self.view.erase(e, sublime.Region(0, self.view.size()))
                    self.view.end_edit(e)
                elif cmd == GDBView.SCROLL:
                    self.view.run_command("goto_line", {"line": data + 1})
                elif cmd == GDBView.VIEWPORT_POSITION:
                    self.view.set_viewport_position(data, True)
                self.queue.task_done()
        except Queue.Empty:
            # get_nowait throws an exception when there's nothing..
            pass
        except:
            traceback.print_exc()
        finally:
            self.view.set_read_only(True)
            if self.doScroll:
                self.view.show(self.view.size())


class GDBValuePairs:
    def __init__(self, string):
        string = string.split("\",")
        self.data = {}
        for pair in string:
            if not "=" in pair:
                continue
            key, value = pair.split("=", 1)
            value = value.replace("\\\"", "'").replace("\"", "")
            self.data[key] = value

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def __str__(self):
        return "%s" % self.data


class GDBVariable:
    def __init__(self, vp=None):
        self.valuepair = vp
        self.children = []
        self.line = 0
        self.is_expanded = False
        if "value" not in vp.data:
            self.update_value()
        self.dirty = False

    def update_value(self):
        line = run_cmd("-var-evaluate-expression %s" % self["name"], True)
        if get_result(line) == "done":
            val = line[line.find("=") + 2:]
            val = val[:val.find("\"")]
            self['value'] = val

    def update(self, d):
        for key in d:
            if key.startswith("new_"):
                self[key[4:]] = d[key]
            elif key == "value":
                self[key] = d[key]

    def get_children(self, name):
        line = run_cmd("-var-list-children 1 \"%s\"" % name, True)
        children = re.split("[, {]+child=\{", line[:line.rfind("}}")])[1:]
        return children

    def add_children(self, name):
        children = self.get_children(name)
        for child in children:
            child = GDBVariable(GDBValuePairs(child[:-1]))
            if child.get_name().endswith(".private") or \
                    child.get_name().endswith(".protected") or \
                    child.get_name().endswith(".public"):
                if child.has_children():
                    self.add_children(child.get_name())
            else:
                self.children.append(child)

    def is_editable(self):
        line = run_cmd("-var-show-attributes %s" % (self.get_name()), True)
        return "editable" in re.findall("(?<=attr=\")[a-z]+(?=\")", line)

    def edit_on_done(self, val):
        line = run_cmd("-var-assign %s \"%s\"" % (self.get_name(), val), True)
        if get_result(line) == "done":
            self.valuepair["value"] = re.search("(?<=value=\")[a-zA-Z0-9]+(?=\")", line).group(0)
            update_variables_view()
        else:
            err = line[line.find("msg=") + 4:]
            sublime.status_message("Error: %s" % err)

    def find(self, name):
        if name == self.get_name():
            return self
        elif self.get_name().startswith(name):
            for child in self.children:
                ret = child.find(name)
                if ret != None:
                    return ret
        return None

    def edit(self):
        sublime.active_window().show_input_panel("New value", self.valuepair["value"], self.edit_on_done, None, None)

    def get_name(self):
        return self.valuepair["name"]

    def expand(self):
        self.is_expanded = True
        if not (len(self.children) == 0 and int(self.valuepair["numchild"]) > 0):
            return
        self.add_children(self.get_name())

    def has_children(self):
        return int(self.valuepair["numchild"]) > 0

    def collapse(self):
        self.is_expanded = False

    def __str__(self):
        return "%s %s = %s" % (self.valuepair['type'], self.valuepair['exp'], self.valuepair['value'])

    def __getitem__(self, key):
        return self.valuepair[key]

    def __setitem__(self, key, value):
        self.valuepair[key] = value
        if key == "value":
            self.dirty = True

    def format(self, indent="", output="", line=0, dirty=[]):
        if self.dirty:
            dirty.append(self)
            self.dirty = False

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


class GDBStackFrame:
    def __init__(self, vp):
        self.valuepairs = vp
        self.args = []

    def __getitem__(self, key):
        return self.valuepairs[key]


def extract_varobjs(line):
    varobjs = line[:line.rfind("}}") + 1]
    varobjs = varobjs.split("varobj=")[1:]
    ret = []
    for varobj in varobjs:
        var = GDBVariable(GDBValuePairs(varobj[1:-1]))
        ret.append(var)
    return ret


def update_variables_view():
    gdb_variables_view.clear()
    output = ""
    line = 0
    dirtylist = []
    for local in gdb_variables:
        output, line = local.format(line=line, dirty=dirtylist)
        gdb_variables_view.add_line(output)
    gdb_variables_view.update()
    regions = []
    v = gdb_variables_view.get_view()
    for dirty in dirtylist:
        regions.append(v.full_line(v.text_point(dirty.line, 0)))
    v.add_regions("sublimegdb.dirtyvariables", regions, "entity.name.class", "", sublime.DRAW_OUTLINED)


def get_variable_at_line(line, var_list):
    if len(var_list) == 0:
        return None

    for i in range(len(var_list)):
        if var_list[i].line == line:
            return var_list[i]
        elif var_list[i].line > line:
            return get_variable_at_line(line, var_list[i - 1].children)
    return get_variable_at_line(line, var_list[len(var_list) - 1].children)


def update_variables(sameFrame):
    global gdb_variables
    if sameFrame:
        line = run_cmd("-var-update --all-values *", True)
        changes = re.split("},", line)
        ret = []
        for i in range(len(changes)):
            change = re.findall("([^=,{}]+)=\"([^\"]+)\"", changes[i])
            d = {}
            for name, value in change:
                d[name] = value
            if "name" in d:
                ret.append(d)
        for value in ret:
            name = value["name"]
            for var in gdb_variables:
                real = var.find(name)
                if real != None:
                    real.update(value)
                    if not "value" in value and not "new_value" in value:
                        real.update_value()
                    break
    else:
        for var in gdb_variables:
            run_cmd("-var-delete %s" % var.get_name())
        line = run_cmd("-stack-list-arguments 0 %d %d" % (gdb_stack_index, gdb_stack_index), True)
        line = line[line.find(",", line.find("{level=")) + 1:]
        args = extract_varnames(line)
        gdb_variables = []
        for arg in args:
            gdb_variables.append(create_variable(arg))
        loc = extract_varnames(run_cmd("-stack-list-locals 0", True))
        for var in loc:
            gdb_variables.append(create_variable(var))
    update_variables_view()


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
        gdb_stackframes.append(GDBStackFrame(GDBValuePairs(frame)))
    return gdb_stackframes


def extract_stackargs(line):
    gdb_stackargs = []
    frames = line.split("level=")[1:]
    for frame in frames:
        curr = []
        args = re.findall("name=\"([^\"]+)\",value=\"([^:\"]+)", frame)
        for arg in args:
            curr.append(arg)
        gdb_stackargs.append(curr)
    return gdb_stackargs


def extract_varnames(line):
    if "}}" in line:
        line = line[:line.rfind("}}")]
    line = line.replace("\"", "").replace("{", "").replace("}", "").replace(",", " ").replace("]", "")
    line = line.split("name=")[1:]
    line = [l.strip() for l in line]
    return line


def create_variable(exp):
    line = run_cmd("-var-create - * %s" % exp, True)
    line = line[line.find(",") + 1:]
    var = GDBValuePairs(line)
    var['exp'] = exp
    return GDBVariable(var)


def update_view_markers(view=None):
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

    if gdb_callstack_view != None:
        view = gdb_callstack_view.get_view()
        view.add_regions("sublimegdb.stackframe", [view.line(view.text_point(gdb_stack_index, 0))], "entity.name.class", "bookmark", sublime.HIDDEN)


count = 0


def run_cmd(cmd, block=False, mimode=True):
    global count
    if not is_running():
        return "0^error,msg=\"no session running\""

    if mimode:
        count = count + 1
        cmd = "%d%s\n" % (count, cmd)
    else:
        cmd = "%s\n\n" % cmd
    log_debug(cmd)
    if gdb_session_view != None:
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
            f = bp["fullname"] if "fullname" in bp.data else bp["file"]
            if not f in newbps:
                newbps[f] = []
            newbps[f].append(int(bp["line"]))
    breakpoints = newbps
    update_view_markers()


def get_result(line):
    return result_regex.search(line).group(0)


def update_cursor():
    global gdb_cursor
    global gdb_cursor_position
    global gdb_stack_frames
    global gdb_stack_index
    global gdb_stack_frame

    currFrame = extract_stackframes(run_cmd("-stack-info-frame", True))[0]
    gdb_stack_index = int(currFrame["level"])
    gdb_cursor = currFrame["fullname"]
    gdb_cursor_position = int(currFrame["line"])
    sublime.active_window().focus_group(get_setting("file_group", 0))
    sublime.active_window().open_file("%s:%d" % (gdb_cursor, gdb_cursor_position), sublime.ENCODED_POSITION)

    sameFrame = gdb_stack_frame != None and gdb_stack_frame["fp"] == currFrame["fp"] and \
                gdb_stack_frame["fullname"] == currFrame["fullname"] and \
                gdb_stack_frame["func"] == currFrame["func"]
    gdb_stack_frame = currFrame
    if not sameFrame:
        line = run_cmd("-stack-list-frames", True)
        if get_result(line) == "error":
            gdb_cursor_position = 0
            update_view_markers()
            return
        gdb_stack_frames = frames = extract_stackframes(line)
        line = run_cmd("-stack-list-arguments 1", True)
        args = extract_stackargs(line)
        gdb_callstack_view.clear()
        for i in range(len(frames)):
            output = "%s(" % frames[i]["func"]
            for v in args[i]:
                output += "%s = %s, " % v
            output += ")\n"

            gdb_callstack_view.add_line(output)
        gdb_callstack_view.update()

    update_view_markers()
    update_variables(sameFrame)


def session_ended_status_message():
    sublime.status_message("GDB session ended")


def gdboutput(pipe):
    global gdb_process
    global gdb_lastresult
    global gdb_lastline
    global gdb_stack_frame
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
                    reason = re.search("(?<=reason=\")[a-zA-Z0-9\-]+(?=\")", line).group(0)
                    if reason.startswith("exited"):
                        run_cmd("-gdb-exit")
                    else:
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
        sublime.set_timeout(session_ended_status_message, 0)
        gdb_stack_frame = None
    global gdb_cursor_position
    gdb_cursor_position = 0
    sublime.set_timeout(update_view_markers, 0)


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
        global gdb_variables_view
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
            if gdb_variables_view == None or gdb_variables_view.is_closed():
                gdb_variables_view = GDBView("GDB Variables", False)
            if gdb_callstack_view == None or gdb_callstack_view.is_closed():
                gdb_callstack_view = GDBView("GDB Callstack")

            gdb_session_view.clear()
            gdb_console_view.clear()
            gdb_variables_view.clear()
            gdb_callstack_view.clear()

            w.set_view_index(gdb_session_view.get_view(), get_setting("session_group", 1), get_setting("session_index", 0))
            w.set_view_index(gdb_console_view.get_view(), get_setting("console_group", 1), get_setting("console_index", 1))
            w.set_view_index(gdb_variables_view.get_view(), get_setting("variables_group", 2), get_setting("variables_index", 0))
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
        update_view_markers(self.view)
        run_cmd("-exec-continue")

    def is_enabled(self):
        return is_running()


class GdbExit(sublime_plugin.TextCommand):
    def run(self, edit):
        wait_until_stopped()
        run_cmd("-gdb-exit", True)

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
        update_view_markers(self.view)


def expand_collapse_variable(view, expand=True, toggle=False):
    row, col = view.rowcol(view.sel()[0].a)
    if gdb_variables_view != None and view.id() == gdb_variables_view.get_view().id():
        var = get_variable_at_line(row, gdb_variables)
        if var and var.has_children():
            if toggle:
                if var.is_expanded:
                    var.collapse()
                else:
                    var.expand()
            elif expand:
                var.expand()
            else:
                var.collapse()
            pos = view.viewport_position()
            update_variables_view()
            gdb_variables_view.update()
            gdb_variables_view.scroll(row)
            gdb_variables_view.set_viewport_position(pos)
            gdb_variables_view.update()


class GdbClick(sublime_plugin.TextCommand):
    def run(self, edit):
        if not is_running():
            return
        row, col = self.view.rowcol(self.view.sel()[0].a)
        if gdb_variables_view != None and self.view.id() == gdb_variables_view.get_view().id():
            expand_collapse_variable(self.view, toggle=True)
        elif gdb_callstack_view != None and self.view.id() == gdb_callstack_view.get_view().id():
            if row < len(gdb_stack_frames):
                run_cmd("-stack-select-frame %d" % row)
                update_cursor()

    def is_enabled(self):
        return is_running()


class GdbCollapseVariable(sublime_plugin.TextCommand):
    def run(self, edit):
        expand_collapse_variable(self.view, expand=False)

    def is_enabled(self):
        if not is_running():
            return False
        row, col = self.view.rowcol(self.view.sel()[0].a)
        if gdb_variables_view != None and self.view.id() == gdb_variables_view.get_view().id():
            return True
        return False


class GdbExpandVariable(sublime_plugin.TextCommand):
    def run(self, edit):
        expand_collapse_variable(self.view)

    def is_enabled(self):
        if not is_running():
            return False
        row, col = self.view.rowcol(self.view.sel()[0].a)
        if gdb_variables_view != None and self.view.id() == gdb_variables_view.get_view().id():
            return True
        return False


class GdbEditVariable(sublime_plugin.TextCommand):
    def run(self, edit):
        row, col = self.view.rowcol(self.view.sel()[0].a)
        if gdb_variables_view != None and self.view.id() == gdb_variables_view.get_view().id():
            var = get_variable_at_line(row, gdb_variables)
            if var.is_editable():
                var.edit()
            else:
                sublime.status_message("Variable isn't editable")

    def is_enabled(self):
        if not is_running():
            return False
        row, col = self.view.rowcol(self.view.sel()[0].a)
        if gdb_variables_view != None and self.view.id() == gdb_variables_view.get_view().id():
            return True
        return False


class GdbEventListener(sublime_plugin.EventListener):
    def on_query_context(self, view, key, operator, operand, match_all):
        if key == "gdb_running":
            return is_running() == operand
        elif key == "gdb_variables_view":
            return gdb_variables_view != None and view.id() == gdb_variables_view.get_view().id()
        return None

    def on_activated(self, view):
        if view.file_name() != None:
            update_view_markers(view)

    def on_load(self, view):
        if view.file_name() != None:
            update_view_markers(view)

    def on_close(self, view):
        if gdb_session_view != None and view.id() == gdb_session_view.get_view().id():
            gdb_session_view.was_closed()
            if is_running():
                wait_until_stopped()
                run_cmd("-gdb-exit", True)
        if gdb_console_view != None and view.id() == gdb_console_view.get_view().id():
            gdb_console_view.was_closed()
        if gdb_variables_view != None and view.id() == gdb_variables_view.get_view().id():
            gdb_variables_view.was_closed()
        if gdb_callstack_view != None and view.id() == gdb_callstack_view.get_view().id():
            gdb_callstack_view.was_closed()
