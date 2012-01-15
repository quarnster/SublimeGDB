import sublime
import sublime_plugin
import subprocess
import threading
import time
import traceback

breakpoints = {}


def update(view):
    #view.add_regions("sublimegdb.breakpoints", breakpoints[view.file_name()], "keyword", "circle", sublime.HIDDEN)
    #if hit_breakpoint:
    view.add_regions("sublimegdb.position", breakpoints[view.file_name()], "entity.name.class", "bookmark", sublime.HIDDEN)


class GdbToggleBreakpoint(sublime_plugin.TextCommand):
    def run(self, edit):
        fn = self.view.file_name()
        if fn not in breakpoints:
            breakpoints[fn] = []
        region = self.view.full_line(self.view.sel()[0])
        if region in breakpoints[fn]:
            breakpoints[fn].remove(region)
        else:
            breakpoints[fn].append(region)
        update(self.view)

gdb_process = None


def gdboutput():
    global gdb_process
    while True:
        try:
            if gdb_process.poll() != None:
                break
            line = gdb_process.stdout.readline().strip()
            if len(line) > 0:
                print "%s" % line
        except:
            traceback.print_exc()

def gdbkill():
    global gdb_process
    time.sleep(2)
    gdb_process.stdin.write("quit\n")
    gdb_process.wait()


def show_input():
    sublime.active_window().show_input_panel("GDB", "quit", input_on_done, input_on_change, input_on_cancel)

class GdbInput(sublime_plugin.TextCommand):
    def run(self, edit):
        show_input()


def input_on_done(s):
    gdb_process.stdin.write("%s\n" % s)

def input_on_cancel():
    pass

def input_on_change(s):
    pass

class GdbLaunch(sublime_plugin.TextCommand):
    def run(self, edit):
        global gdb_process
        if gdb_process == None or gdb_process.poll() != None:
            gdb_process = subprocess.Popen("gdb", shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE)

            t = threading.Thread(target=gdboutput)
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




