import sublime
import sublime_plugin
import subprocess
import threading
import time
import traceback
import sys
import os

breakpoints = {}


def update(view):
    bps = []
    for line in breakpoints[view.file_name()]:
        bps.append(view.full_line(view.text_point(line - 1, 0)))
    view.add_regions("sublimegdb.breakpoints", bps, "keyword.gdb", "circle", sublime.HIDDEN)
    #if hit_breakpoint:

    # cursor: view.add_regions("sublimegdb.position", breakpoints[view.file_name()], "entity.name.class", "bookmark", sublime.HIDDEN)

def add_breakpoint(filename, line):
    breakpoints[filename].append(line)

def remove_breakpoint(filename, line):
    breakpoints[filename].remove(line)

def toggle_breakpoint(filename, line):
    if line in breakpoints[filename]:
        remove_breakpoint(filename, line)
    else:
        add_breakpoint(filename, line)

def sync_breakpoints():
    for file in breakpoints:
        for bp in breakpoints[file]:
            cmd = "break %s:%d\n" % (file, bp)
            gdb_process.stdin.write(cmd)


class GdbToggleBreakpoint(sublime_plugin.TextCommand):
    def run(self, edit):
        fn = self.view.file_name()
        if fn not in breakpoints:
            breakpoints[fn] = []

        line, col = self.view.rowcol(self.view.sel()[0].a)
        toggle_breakpoint(fn, line + 1)
        update(self.view)

gdb_process = None
lock = threading.Lock()
output = []

def get_view():
    gdb_view = sublime.active_window().open_file("GDB Session")
    gdb_view.set_scratch(True)
    gdb_view.set_read_only(True)
    return gdb_view

def update_view():
    global output
    lock.acquire()
    try:
        gdb_view = get_view()
        if (gdb_view.is_loading()):
            sublime.set_timeout(update_view, 100)
            return

        e = gdb_view.begin_edit()
        try:
            gdb_view.set_read_only(False)
            for line in output:
                gdb_view.insert(e, gdb_view.size(), line)
            gdb_view.set_read_only(True)
            gdb_view.show(gdb_view.size())
            output = []
        finally:
            gdb_view.end_edit(e)
    finally:
        lock.release()



def gdboutput(pipe):
    global gdb_process
    global old_stdin
    global lock
    global output
    while True:
        try:
            if gdb_process.poll() != None:
                break
            line = pipe.readline().replace("(gdb)", "").strip()
            if len(line) > 0:
                lock.acquire()
                output.append("%s\n" % line)
                lock.release()
                sublime.set_timeout(update_view, 0)
        except:
            traceback.print_exc()
    if pipe == gdb_process.stdout:
        lock.acquire()
        output.append("GDB session ended\n")
        lock.release()
        sublime.set_timeout(update_view, 0)


def show_input():
    sublime.active_window().show_input_panel("GDB", "", input_on_done, input_on_change, input_on_cancel)

class GdbInput(sublime_plugin.TextCommand):
    def run(self, edit):
        show_input()


def input_on_done(s):
    gdb_process.stdin.write("%s\n" % s)
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



class GdbLaunch(sublime_plugin.TextCommand):
    def run(self, edit):
        global gdb_process
        if gdb_process == None or gdb_process.poll() != None:
            os.chdir(get_setting("workingdir", "/tmp"))
            gdb_process = subprocess.Popen(get_setting("commandline"), shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            sync_breakpoints()

            t = threading.Thread(target=gdboutput, args=(gdb_process.stdout,))
            t.start()

            t = threading.Thread(target=gdboutput, args=(gdb_process.stderr,))
            t.start()

            show_input()
        else:
            sublime.status_message("GDB is already running!")

class GdbEventListener(sublime_plugin.EventListener):
    def on_query_context(self, view, key, operator, operand, match_all):
        global gdb_process
        if key != "gdb_running":
            return None
        running = gdb_process != None and gdb_process.poll() == None
        return running == operand




