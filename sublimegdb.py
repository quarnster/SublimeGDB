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
from resultparser import parse_result_line
from types import ListType


def get_setting(key, default=None):
    try:
        s = sublime.active_window().active_view().settings()
        if s.has("sublimegdb_%s" % key):
            return s.get("sublimegdb_%s" % key)
    except:
        pass
    return sublime.load_settings("SublimeGDB.sublime-settings").get(key, default)

DEBUG = get_setting("debug", False)
DEBUG_FILE = get_setting("debug_file", "/tmp/sublimegdb.txt")

breakpoints = {}
gdb_lastresult = ""
gdb_lastline = ""
gdb_cursor = ""
gdb_cursor_position = 0

gdb_process = None
gdb_stack_frame = None
gdb_stack_index = 0

gdb_run_status = None
gdb_session_view = None
gdb_console_view = None
gdb_variables_view = None
gdb_callstack_view = None
gdb_register_view = None
gdb_views = []
result_regex = re.compile("(?<=\^)[^,]*")


def log_debug(line):
    if DEBUG:
        os.system("echo \"%s\" >> \"%s\"" % (line, DEBUG_FILE))


class GDBView(object):
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

    def set_syntax(self, syntax):
        self.get_view().set_syntax_file(syntax)

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


class GDBVariable:
    def __init__(self, vp=None):
        self.valuepair = vp
        self.children = []
        self.line = 0
        self.is_expanded = False
        if "value" not in vp:
            self.update_value()
        self.dirty = False
        self.deleted = False

    def delete(self):
        run_cmd("-var-delete %s" % self.get_name())
        self.deleted = True

    def update_value(self):
        line = run_cmd("-var-evaluate-expression %s" % self["name"], True)
        if get_result(line) == "done":
            self['value'] = parse_result_line(line)["value"]

    def update(self, d):
        for key in d:
            if key.startswith("new_"):
                if key == "new_num_children":
                    self["numchild"] = d[key]
                else:
                    self[key[4:]] = d[key]
            elif key == "value":
                self[key] = d[key]

    def add_children(self, name):
        children = listify(parse_result_line(run_cmd("-var-list-children 1 \"%s\"" % name, True))["children"]["child"])
        for child in children:
            child = GDBVariable(child)
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
            self.valuepair["value"] = parse_result_line(line)["value"]
            gdb_variables_view.update_view()
        else:
            err = line[line.find("msg=") + 4:]
            sublime.status_message("Error: %s" % err)

    def find(self, name):
        if self.deleted:
            return None
        if name == self.get_name():
            return self
        elif name.startswith(self.get_name()):
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
        if not "dynamic_type" in self or len(self['dynamic_type']) == 0 or self['dynamic_type'] == self['type']:
            return "%s %s = %s" % (self['type'], self['exp'], self['value'])
        else:
            return "%s %s = (%s) %s" % (self['type'], self['exp'], self['dynamic_type'], self['value'])

    def __iter__(self):
        return self.valuepair.__iter__()

    def __getitem__(self, key):
        return self.valuepair[key]

    def __setitem__(self, key, value):
        self.valuepair[key] = value
        if key == "value":
            self.dirty = True

    def clear_dirty(self):
        self.dirty = False
        for child in self.children:
            child.clear_dirty()

    def is_dirty(self):
        dirt = self.dirty
        if not dirt and not self.is_expanded:
            for child in self.children:
                if child.is_dirty():
                    dirt = True
                    break
        return dirt

    def format(self, indent="", output="", line=0, dirty=[]):
        icon = " "
        if self.has_children():
            if self.is_expanded:
                icon = "-"
            else:
                icon = "+"

        output += "%s%s%s\n" % (indent, icon, self)
        self.line = line
        line = line + 1
        indent += "    "
        if self.is_expanded:
            for child in self.children:
                output, line = child.format(indent, output, line, dirty)
        if self.is_dirty():
            dirty.append(self)
        return (output, line)


class GDBRegister:
    def __init__(self, name, index, val):
        self.name = name
        self.index = index
        self.value = val


class GDBRegisterView(GDBView):
    def __init__(self):
        super(GDBRegisterView, self).__init__("GDB Registers")
        self.names = None

    def get_names(self):
        line = run_cmd("-data-list-register-names", True)
        return parse_result_line(line)["register-names"]

    def get_values(self):
        line = run_cmd("-data-list-register-values x", True)
        if get_result(line) != "done":
            return []
        return parse_result_line(line)["register-values"]

    def update_values(self):
        if self.names == None:
            self.names = self.get_names()
        self.values = self.get_values()
        self.clear()
        for item in self.values:
            log_debug(item)
            index = int(item["number"])
            self.add_line("%s: %s\n" % (self.names[index], item["value"]))


class GDBVariablesView(GDBView):
    def __init__(self):
        super(GDBVariablesView, self).__init__("GDB Variables", False)
        self.variables = []

    def update_view(self):
        self.clear()
        output = ""
        line = 0
        dirtylist = []
        for local in self.variables:
            output, line = local.format(line=line, dirty=dirtylist)
            self.add_line(output)
        self.update()
        regions = []
        v = self.get_view()
        for dirty in dirtylist:
            regions.append(v.full_line(v.text_point(dirty.line, 0)))
        v.add_regions("sublimegdb.dirtyvariables", regions,
                        get_setting("changed_variable_scope", "entity.name.class"),
                        get_setting("changed_variable_icon", ""),
                        sublime.DRAW_OUTLINED)

    def extract_varnames(self, res):
        if "name" in res:
            return listify(res["name"])
        elif len(res) > 0 and type(res) is ListType:
            if "name" in res[0]:
                return [x["name"] for x in res]
        return []

    def create_variable(self, exp):
        line = run_cmd("-var-create - * %s" % exp, True)
        var = parse_result_line(line)
        var['exp'] = exp
        return GDBVariable(var)

    def update_variables(self, sameFrame):
        if sameFrame:
            for var in self.variables:
                var.clear_dirty()
            ret = parse_result_line(run_cmd("-var-update --all-values *", True))["changelist"]
            if "varobj" in ret:
                ret = listify(ret["varobj"])
            dellist = []
            for value in ret:
                name = value["name"]
                for var in self.variables:
                    real = var.find(name)
                    if real != None:
                        if  "in_scope" in value and value["in_scope"] == "false":
                            real.delete()
                            dellist.append(real)
                            continue
                        real.update(value)
                        if not "value" in value and not "new_value" in value:
                            real.update_value()
                        break
            for item in dellist:
                self.variables.remove(item)

            loc = self.extract_varnames(parse_result_line(run_cmd("-stack-list-locals 0", True))["locals"])
            tracked = []
            for var in loc:
                create = True
                for var2 in self.variables:
                    if var2['exp'] == var and var2 not in tracked:
                        tracked.append(var2)
                        create = False
                        break
                if create:
                    self.variables.append(self.create_variable(var))
        else:
            for var in self.variables:
                var.delete()
            args = self.extract_varnames(parse_result_line(run_cmd("-stack-list-arguments 0 %d %d" % (gdb_stack_index, gdb_stack_index), True))["stack-args"]["frame"]["args"])
            self.variables = []
            for arg in args:
                self.variables.append(self.create_variable(arg))
            loc = self.extract_varnames(parse_result_line(run_cmd("-stack-list-locals 0", True))["locals"])
            for var in loc:
                self.variables.append(self.create_variable(var))
        self.update_view()

    def get_variable_at_line(self, line, var_list=None):
        if var_list == None:
            var_list = self.variables
        if len(var_list) == 0:
            return None

        for i in range(len(var_list)):
            if var_list[i].line == line:
                return var_list[i]
            elif var_list[i].line > line:
                return self.get_variable_at_line(line, var_list[i - 1].children)
        return self.get_variable_at_line(line, var_list[len(var_list) - 1].children)


class GDBCallstackView(GDBView):
    def __init__(self):
        super(GDBCallstackView, self).__init__("GDB Callstack")

    def update_callstack(self):
        global gdb_cursor_position
        line = run_cmd("-stack-list-frames", True)
        if get_result(line) == "error":
            gdb_cursor_position = 0
            update_view_markers()
            return
        self.frames = listify(parse_result_line(line)["stack"]["frame"])
        args = listify(parse_result_line(run_cmd("-stack-list-arguments 1", True))["stack-args"]["frame"])
        self.clear()
        for i in range(len(self.frames)):
            output = "%s(" % self.frames[i]["func"]
            for arg in args[i]["args"]:
                output += "%s = %s, " % (arg["name"], arg["value"])
            output += ");\n"

            self.add_line(output)
        self.update()

    def select(self, row):
        if row < len(self.frames):
            run_cmd("-stack-select-frame %d" % row)
            update_cursor()


def extract_breakpoints(line):
    res = parse_result_line(line)
    if "bkpt" in res["BreakpointTable"]:
        return res["BreakpointTable"]["bkpt"]
    else:
        return res["BreakpointTable"]["body"]["bkpt"]


def update_view_markers(view=None):
    if view == None:
        view = sublime.active_window().active_view()
    bps = []
    fn = view.file_name()
    if fn in breakpoints:
        for line in breakpoints[fn]:
            if not (line == gdb_cursor_position and fn == gdb_cursor):
                bps.append(view.full_line(view.text_point(line - 1, 0)))
    view.add_regions("sublimegdb.breakpoints", bps,
                        get_setting("breakpoint_scope", "keyword.gdb"),
                        get_setting("breakpoint_icon", "circle"),
                        sublime.HIDDEN)
    cursor = []

    if fn == gdb_cursor and gdb_cursor_position != 0:
        cursor.append(view.full_line(view.text_point(gdb_cursor_position - 1, 0)))

    pos_scope = get_setting("position_scope", "entity.name.class")
    pos_icon = get_setting("position_icon", "bookmark")
    view.add_regions("sublimegdb.position", cursor, pos_scope, pos_icon, sublime.HIDDEN)

    if gdb_callstack_view != None:
        view = gdb_callstack_view.get_view()
        view.add_regions("sublimegdb.stackframe",
                            [view.line(view.text_point(gdb_stack_index, 0))],
                            pos_scope, pos_icon, sublime.HIDDEN)


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
        i = 0
        while not gdb_lastresult.startswith(countstr) and i < 100:
            i += 1
            time.sleep(0.1)
        if i >= 100:
            raise ValueError("Command \"%s\" took longer than 10 seconds to perform?" % cmd)
        return gdb_lastresult
    return count


def wait_until_stopped():
    if gdb_run_status == "running":
        result = run_cmd("-exec-interrupt --all", True)
        if "^done" in result:
            i = 0
            while not "stopped" in gdb_run_status and i < 100:
                i = i + 1
                time.sleep(0.1)
            if i >= 100:
                print "I'm confused... I think status is %s, but it seems it wasn't..." % gdb_run_status
                return False
            return True
    return False


def resume():
    global gdb_run_status
    gdb_run_status = "running"
    run_cmd("-exec-continue", True)


def add_breakpoint(filename, line):
    if is_running():
        res = wait_until_stopped()
        line = int(parse_result_line(run_cmd("-break-insert %s:%d" % (filename, line), True))["bkpt"]["line"])
        if res:
            resume()
    breakpoints[filename].append(line)


def remove_breakpoint(filename, line):
    breakpoints[filename].remove(line)
    if is_running():
        res = wait_until_stopped()
        gdb_breakpoints = extract_breakpoints(run_cmd("-break-list", True))
        for bp in gdb_breakpoints:
            fn = bp["fullname"] if "fullname" in bp else bp["file"]
            if fn == filename and bp["line"] == str(line):
                run_cmd("-break-delete %s" % bp["number"])
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
            bp = parse_result_line(out)["bkpt"]
            f = bp["fullname"] if "fullname" in bp else bp["file"]
            if not f in newbps:
                newbps[f] = []
            newbps[f].append(int(bp["line"]))
    breakpoints = newbps
    update_view_markers()


def get_result(line):
    return result_regex.search(line).group(0)


def listify(var):
    if not type(var) is ListType:
        return [var]
    return var


def update_cursor():
    global gdb_cursor
    global gdb_cursor_position
    global gdb_stack_index
    global gdb_stack_frame

    currFrame = parse_result_line(run_cmd("-stack-info-frame", True))["frame"]
    gdb_stack_index = int(currFrame["level"])
    gdb_cursor = currFrame["fullname"]
    gdb_cursor_position = int(currFrame["line"])
    sublime.active_window().focus_group(get_setting("file_group", 0))
    file_view = sublime.active_window().open_file("%s:%d" % (gdb_cursor, gdb_cursor_position), sublime.ENCODED_POSITION)

    sameFrame = gdb_stack_index != 0 or \
                (gdb_stack_frame != None and \
                gdb_stack_frame["fullname"] == currFrame["fullname"] and \
                gdb_stack_frame["func"] == currFrame["func"])
    gdb_stack_frame = currFrame
    if not sameFrame:
        gdb_callstack_view.update_callstack()

    syntax = file_view.settings().get("syntax")
    gdb_variables_view.set_syntax(syntax)
    gdb_callstack_view.set_syntax(syntax)
    gdb_register_view.set_syntax(syntax)

    update_view_markers()
    gdb_variables_view.update_variables(sameFrame)
    gdb_register_view.update_values()


def session_ended_status_message():
    sublime.status_message("GDB session ended")


def gdboutput(pipe):
    global gdb_process
    global gdb_lastresult
    global gdb_lastline
    global gdb_stack_frame
    global gdb_run_status
    command_result_regex = re.compile("^\d+\^")
    run_status_regex = re.compile("(^\d*\*)([^,]+)")
    while True:
        try:
            if gdb_process.poll() != None:
                break
            line = pipe.readline().strip()

            if len(line) > 0:
                log_debug(line)
                gdb_session_view.add_line("%s\n" % line)

                run_status = run_status_regex.match(line)
                if run_status != None:
                    gdb_run_status = run_status.group(2)
                    reason = re.search("(?<=reason=\")[a-zA-Z0-9\-]+(?=\")", line)
                    if reason != None and reason.group(0).startswith("exited"):
                        run_cmd("-gdb-exit")
                    elif not "running" in gdb_run_status:
                        sublime.set_timeout(update_cursor, 0)
                if not line.startswith("(gdb)"):
                    gdb_lastline = line
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
        global gdb_run_status
        global gdb_register_view
        global gdb_views
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
            session_group = get_setting("session_group", 1)
            console_group = get_setting("console_group", 1)
            variables_group = get_setting("variables_group", 1)
            callstack_group = get_setting("callstack_group", 2)
            register_group = get_setting("register_group", 2)
            gdb_views = []

            if gdb_session_view == None or gdb_session_view.is_closed():
                w.focus_group(session_group)
                gdb_session_view = GDBView("GDB Session")
            if gdb_console_view == None or gdb_console_view.is_closed():
                w.focus_group(console_group)
                gdb_console_view = GDBView("GDB Console")
            if gdb_variables_view == None or gdb_variables_view.is_closed():
                w.focus_group(variables_group)
                gdb_variables_view = GDBVariablesView()
            if gdb_callstack_view == None or gdb_callstack_view.is_closed():
                w.focus_group(callstack_group)
                gdb_callstack_view = GDBCallstackView()
            if gdb_register_view == None or gdb_register_view.is_closed():
                w.focus_group(register_group)
                gdb_register_view = GDBRegisterView()

            gdb_views.append(gdb_session_view)
            gdb_views.append(gdb_console_view)
            gdb_views.append(gdb_variables_view)
            gdb_views.append(gdb_callstack_view)
            gdb_views.append(gdb_register_view)
            for view in gdb_views:
                view.clear()
            # setting the view index keeps crashing my Linux...
            #w.set_view_index(gdb_session_view.get_view(), session_group, get_setting("session_index", 0))
            #w.set_view_index(gdb_console_view.get_view(), console_group, get_setting("console_index", 1))
            #w.set_view_index(gdb_variables_view.get_view(), variables_group, get_setting("variables_index", 2))
            #w.set_view_index(gdb_callstack_view.get_view(), callstack_group, get_setting("callstack_index", 0))
            t = threading.Thread(target=gdboutput, args=(gdb_process.stdout,))
            t.start()
            try:
                run_cmd("-gdb-show interpreter", True)
            except:
                sublime.error_message("""\
It seems you're not running gdb with the "mi" interpreter. Please add
"--interpreter=mi" to your gdb command line""")
                gdb_process.stdin.write("quit\n")
                return

            run_cmd("-gdb-set target-async 1")
            run_cmd("-gdb-set pagination off")
            run_cmd("-gdb-set non-stop on")

            sync_breakpoints()
            gdb_run_status = "running"
            run_cmd(get_setting("exec_cmd"), "-exec-run", True)

            show_input()
        else:
            sublime.status_message("GDB is already running!")

    def is_enabled(self):
        return not is_running()

    def is_visible(self):
        return not is_running()


class GdbContinue(sublime_plugin.TextCommand):
    def run(self, edit):
        global gdb_cursor_position
        gdb_cursor_position = 0
        update_view_markers(self.view)
        resume()

    def is_enabled(self):
        return is_running() and gdb_run_status != "running"

    def is_visible(self):
        return is_running()


class GdbExit(sublime_plugin.TextCommand):
    def run(self, edit):
        wait_until_stopped()
        run_cmd("-gdb-exit", True)

    def is_enabled(self):
        return is_running()

    def is_visible(self):
        return is_running()


class GdbPause(sublime_plugin.TextCommand):
    def run(self, edit):
        run_cmd("-gdb-interrupt")

    def is_enabled(self):
        return is_running()

    def is_visible(self):
        return is_running()


class GdbStepOver(sublime_plugin.TextCommand):
    def run(self, edit):
        run_cmd("-exec-next")

    def is_enabled(self):
        return is_running() and gdb_run_status != "running"

    def is_visible(self):
        return is_running()


class GdbStepInto(sublime_plugin.TextCommand):
    def run(self, edit):
        run_cmd("-exec-step")

    def is_enabled(self):
        return is_running() and gdb_run_status != "running"

    def is_visible(self):
        return is_running()


class GdbNextInstruction(sublime_plugin.TextCommand):
    def run(self, edit):
        run_cmd("-exec-next-instruction")

    def is_enabled(self):
        return is_running() and gdb_run_status != "running"

    def is_visible(self):
        return is_running()


class GdbStepOut(sublime_plugin.TextCommand):
    def run(self, edit):
        run_cmd("-exec-finish")

    def is_enabled(self):
        return is_running() and gdb_run_status != "running"

    def is_visible(self):
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
        var = gdb_variables_view.get_variable_at_line(row)
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
            gdb_variables_view.update_view()
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
            gdb_callstack_view.select(row)

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
            var = gdb_variables_view.get_variable_at_line(row)
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
        for v in gdb_views:
            if view.id() == v.get_view().id():
                v.was_closed()
