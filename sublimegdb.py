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


breakpoints = {}
gdb_breakpoints = []
gdb_stackframes = []
gdb_lastresult = ""
gdb_lastline = ""
gdb_cursor = ""


class GDBView:
    def __init__(self, name):
        self.queue = Queue.Queue()
        self.name = name
        self.create_view()

    def add_line(self, line):
        self.queue.put(line)
        sublime.set_timeout(self.update, 0)

    def create_view(self):
        self.view = sublime.active_window().new_file()
        self.view.set_name(self.name)
        self.view.set_scratch(True)
        self.view.set_read_only(True)

    def update(self):
        print "window: %s" % self.view.window()
        e = self.view.begin_edit()
        self.view.set_read_only(False)
        try:
            while True:
                line = self.queue.get_nowait()
                self.view.insert(e, self.view.size(), line)
                self.queue.task_done()
        except:
            pass
        finally:
            self.view.end_edit(e)
            self.view.set_read_only(True)
            self.view.show(self.view.size())

class GDBValuePairs:
    def __init__(self, string):
        string = string.split(",")
        self.data = {}
        for pair in string:
            key, value = pair.split("=")
            value = value.replace("\"", "")
            self.data[key] = value


def extract_breakpoints(line):
    global gdb_breakpoints
    gdb_breakpoints = []
    bps = re.findall("(?<=,bkpt\=\{)[a-zA-Z,=/\"0-9.]+", line)
    for bp in bps:
        gdb_breakpoints.append(GDBValuePairs(bp))


def extract_stackframes(line):
    global gdb_stackframes
    gdb_stackframes = []
    frames = re.findall("(?<=frame\=\{)[a-zA-Z,=/\"0-9.]+", line)
    for frame in frames:
        gdb_stackframes.append(GDBValuePairs(frame))


def update(view):
    bps = []
    for line in breakpoints[view.file_name()]:
        bps.append(view.full_line(view.text_point(line - 1, 0)))
    view.add_regions("sublimegdb.breakpoints", bps, "keyword.gdb", "circle", sublime.HIDDEN)
    #if hit_breakpoint:
    # cursor: view.add_regions("sublimegdb.position", breakpoints[view.file_name()], "entity.name.class", "bookmark", sublime.HIDDEN)

count = 0


def run_cmd(cmd, block=False):
    global count
    count = count + 1
    cmd = "%d%s\n" % (count, cmd)
    output.put(cmd)
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
        run_cmd("-break-list", True)
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
    for file in breakpoints:
        for bp in breakpoints[file]:
            cmd = "-break-insert %s:%d" % (file, bp)
            run_cmd(cmd)


gdb_process = None
gdb_session_view = None
gdb_console_view = None


def gdboutput(pipe):
    global gdb_process
    global gdb_lastresult
    global gdb_lastline
    command_result_regex = re.compile("^\d+\^")
    while True:
        try:
            if gdb_process.poll() != None:
                break
            line = pipe.readline().strip()

            if len(line) > 0:
                gdb_session_view.add_line("%s\n" % line)


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


def show_input():
    sublime.active_window().show_input_panel("GDB", "", input_on_done, input_on_change, input_on_cancel)


class GdbInput(sublime_plugin.TextCommand):
    def run(self, edit):
        show_input()


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


class GdbLaunch(sublime_plugin.TextCommand):
    def run(self, edit):
        global gdb_process
        global gdb_session_view
        global gdb_console_view
        if gdb_process == None or gdb_process.poll() != None:
            os.chdir(get_setting("workingdir", "/tmp"))
            commandline = get_setting("commandline")
            commandline.insert(1, "--interpreter=mi")
            gdb_process = subprocess.Popen(commandline, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            gdb_session_view = GDBView("GDB Session")
            gdb_console_view = GDBView("GDB Console")
            sync_breakpoints()
            gdb_process.stdin.write("-exec-run\n")

            t = threading.Thread(target=gdboutput, args=(gdb_process.stdout,))
            t.start()

            show_input()
        else:
            sublime.status_message("GDB is already running!")


class GdbExit(sublime_plugin.TextCommand):
    def run(self, edit):
        run_cmd("-gdb-exit")


class GdbPause(sublime_plugin.TextCommand):
    def run(self, edit):
        run_cmd("-gdb-interrupt")


class GdbNext(sublime_plugin.TextCommand):
    def run(self, edit):
        run_cmd("-exec-next")


class GdbNextInstruction(sublime_plugin.TextCommand):
    def run(self, edit):
        run_cmd("-exec-next-instruction")


class GdbStepOut(sublime_plugin.TextCommand):
    def run(self, edit):
        run_cmd("-exec-finish")


class GdbToggleBreakpoint(sublime_plugin.TextCommand):
    def run(self, edit):
        fn = self.view.file_name()
        if fn not in breakpoints:
            breakpoints[fn] = []

        line, col = self.view.rowcol(self.view.sel()[0].a)
        toggle_breakpoint(fn, line + 1)
        update(self.view)


class GdbEventListener(sublime_plugin.EventListener):
    def on_close(self, view):
        global gdb_view
        if view == gdb_view:
            gdb_view = None

    def on_query_context(self, view, key, operator, operand, match_all):
        global gdb_process
        if key != "gdb_running":
            return None
        return is_running() == operand


