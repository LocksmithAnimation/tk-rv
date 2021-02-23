import os
import subprocess

working_dir = os.path.dirname(os.path.abspath(__file__))
py_dir = os.path.dirname(working_dir)
py_dir = os.path.join(py_dir, "python")
py_dir = os.path.join(py_dir, [x for x in os.listdir(py_dir) if os.path.isdir(os.path.join(py_dir, x))][0])
py_dir = os.path.join(py_dir, "ui")

ui_files = [x for x in os.listdir(working_dir) if not os.path.isdir(os.path.join(working_dir, x)) and "." in x and x.split(".")[-1] == "ui"]

if "resources.qrc" in os.listdir(working_dir):
    
    ui_path = os.path.join(working_dir, "resources.qrc")    
    py_path = os.path.join(py_dir, "resources_rc.py")
    
    command = "pyside-rcc -py3 -o " + py_path + " " + ui_path
    subprocess.call(command)
    
    data = None
    
    with open(py_path) as f:
        data = f.read()
    f.closed
    
    data = data.replace("PySide", "tank.platform.qt")
    
    with open(py_path, "w") as f:
        f.write(data)
    f.closed
    
for ui_file in ui_files:

    ui_path = os.path.join(working_dir, ui_file)
    py_path = os.path.join(py_dir, ui_file.replace(".ui", ".py"))
    
    command = "pyside-uic --from-imports -o " + py_path + " " + ui_path
    print(command)
    subprocess.call(command)
    
    data = None
    
    with open(py_path) as f:
        data = f.read()
    f.closed
    
    data = data.replace("PySide", "tank.platform.qt")
    
    with open(py_path, "w") as f:
        f.write(data)
    f.closed
    
    #from tank.platform.qt import QtCore, QtGui
