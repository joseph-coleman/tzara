# More Examples

Here are some Jupyter examples showcasing widgets and interactive plots and other things. 

---
[TOC]

---

# HTML

You can use python to embed any sort of HTML in your page dynamically.  

```jupyter
from IPython.display import HTML, display

display(HTML("""<style>@keyframes gradientShift {0% { background-position: 50% 0%; } 50% { background-position: 50% 100%; } 100% { background-position: 50% 0%; }}
                .custom_header {
                background: linear-gradient(270deg, #FFCC00DD, #00CCFFDD), url('/favicon.ico');
                background-position:center;
                background-repeat: no-repeat;
                background-size: cover;
                animation: gradientShift 30s ease infinite;
                color:#FFFFFF;padding:3em;border:4px solid #002D72;font-size:250%;padding:0 0 0 20px;
                }
             </style>"""))

def header(txt, n=1):
    display(HTML(f"""
    <div class='custom_header'>
    <h{n}>{txt}</h{n}>
    </div>
    """))
header("hello world")
```

# Streaming

This example demonstrates data streaming messages from the kernel.

```jupyter
import time

for x in "Hello World!":
    print(x, end="")
    time.sleep(0.5)
```

# Installing

If you need to install something, put a pip command in its own block, or just temporarily edit an existing block, type your pip command, hit run, then clear output to get back to the original code. 

For example, say you also wanted to install `polars`.  You can click the Edit button ✏️, type `pip install polars`, click Run button ▶️, then click Clear Output button 🗑️, and you're back to the original code.

A `pip install` lands in the jupyter container's filesystem, so it outlives the kernel getting reaped, but it is gone the next time the container is rebuilt.  For anything you want to keep, add it to `Dockerfile.jupyter` and rebuild. 

```jupyter
pip install pandas
```

# Widgets

Two libraries, partial support.  

## ipywidgets

Support is **not** complete - styling in particular is just not available - but the interactivity largely is, so you can create some interactive pages if so desired.  See <https://ipywidgets.readthedocs.io/en/latest/> for more information, and bring some of that stuff into your Jupyter notebooks if you haven't already!

What's supported:

| Group | Widgets |
|-------|---------|
| Selection | `Dropdown`, `RadioButtons`, `Select`, `SelectMultiple`, `ToggleButtons`, `SelectionSlider`, `Combobox` |
| Numeric | `IntSlider`, `FloatSlider`, `IntRangeSlider`, `FloatRangeSlider`, `FloatLogSlider`, `IntText`, `FloatText`, `BoundedIntText`, `BoundedFloatText` |
| Progress | `IntProgress`, `FloatProgress` |
| Boolean | `Checkbox`, `ToggleButton`, `Valid` |
| Text | `Text`, `Textarea`, `Password`, `Label`, `HTML`, `HTMLMath` |
| Pickers | `DatePicker`, `TimePicker`, `ColorPicker` |
| Containers | `VBox`, `HBox`, `GridBox`, `Tab`, `Accordion`, `Stack` |
| Media | `Image`, `Audio`, `Video` |
| Other | `Button`, `Output`, `Play`, `FileUpload` |

`jslink` and `jsdlink` work as well.  The big cell below exercises most of these.

```jupyter
import ipywidgets as widgets
from IPython.display import display, HTML

# ============================================================
# Helper: section headers
# ============================================================
def section(title):
    display(HTML(f"<h3 style='border-bottom:2px solid #888; padding-bottom:4px;'>{title}</h3>"))


# ============================================================
# 1. SELECTION WIDGETS
# ============================================================
section("Selection Widgets")

# RadioButtons
radio = widgets.RadioButtons(
    options=["Apple", "Banana", "Cherry"],
    value="Banana",
    description="Fruit:",
)
display(radio)

# Select (single)
select = widgets.Select(
    options=["Linux", "macOS", "Windows"],
    value="Linux",
    rows=3,
    description="OS:",
)
display(select)

# SelectMultiple
select_multi = widgets.SelectMultiple(
    options=["Red", "Green", "Blue", "Yellow"],
    value=["Red", "Blue"],
    rows=4,
    description="Colors:",
)
display(select_multi)

# ToggleButtons
toggle_btns = widgets.ToggleButtons(
    options=["Slow", "Medium", "Fast"],
    description="Speed:",
    button_style="info",
    tooltips=["Tortoise", "Hare", "Cheetah"],
)
display(toggle_btns)

# SelectionSlider
sel_slider = widgets.SelectionSlider(
    options=["cold", "cool", "warm", "hot", "blazing"],
    value="warm",
    description="Temperature:",
)
display(sel_slider)

# Combobox
combo = widgets.Combobox(
    placeholder="Type or pick...",
    options=["PostgreSQL", "MySQL", "SQLite", "Redis"],
    description="Database:",
    ensure_option=False,
)
display(combo)

# Dropdown (already supported - for comparison)
dropdown = widgets.Dropdown(
    options=["Option A", "Option B", "Option C"],
    value="Option B",
    description="Dropdown:",
)
display(dropdown)


# ============================================================
# 2. NUMERIC WIDGETS
# ============================================================
section("Numeric Widgets")

# BoundedIntText
bounded_int = widgets.BoundedIntText(
    value=7,
    min=0,
    max=10,
    step=1,
    description="Bounded Int:",
)
display(bounded_int)

# BoundedFloatText
bounded_float = widgets.BoundedFloatText(
    value=3.14,
    min=0.0,
    max=10.0,
    step=0.01,
    description="Bounded Float:",
)
display(bounded_float)

# IntRangeSlider
int_range = widgets.IntRangeSlider(
    value=[20, 80],
    min=0,
    max=100,
    step=5,
    description="Int Range:",
)
display(int_range)

# FloatRangeSlider
float_range = widgets.FloatRangeSlider(
    value=[0.2, 0.8],
    min=0.0,
    max=1.0,
    step=0.01,
    description="Float Range:",
)
display(float_range)

# FloatLogSlider
log_slider = widgets.FloatLogSlider(
    value=1.0,
    base=10,
    min=-2,
    max=4,
    step=0.2,
    description="Log Scale:",
)
display(log_slider)

# IntSlider (already supported - for comparison)
int_slider = widgets.IntSlider(value=50, min=0, max=100, description="Int Slider:")
display(int_slider)


# ============================================================
# 3. TEXT WIDGETS
# ============================================================
section("Text Widgets")

# Password
password = widgets.Password(
    value="",
    placeholder="Enter password",
    description="Password:",
)
display(password)

# Text (already supported - for comparison)
text = widgets.Text(value="Hello", description="Text:")
display(text)

# Textarea (already supported - for comparison)
textarea = widgets.Textarea(value="Line 1\nLine 2", description="Textarea:", rows=3)
display(textarea)


# ============================================================
# 4. DATE / TIME / COLOR PICKERS
# ============================================================
section("Pickers")

# DatePicker
date_picker = widgets.DatePicker(
    description="Pick a date:",
)
display(date_picker)

# TimePicker
time_picker = widgets.TimePicker(
    description="Pick a time:",
)
display(time_picker)

# ColorPicker
color_picker = widgets.ColorPicker(
    concise=False,
    description="Color:",
    value="#1e90ff",
)
display(color_picker)

# ColorPicker (concise)
color_concise = widgets.ColorPicker(
    concise=True,
    description="Concise:",
    value="#ff6347",
)
display(color_concise)


# ============================================================
# 5. PROGRESS BARS
# ============================================================
section("Progress Bars")

# IntProgress
int_progress = widgets.IntProgress(
    value=70,
    min=0,
    max=100,
    description="Loading:",
    bar_style="success",
)
display(int_progress)

# FloatProgress
float_progress = widgets.FloatProgress(
    value=0.45,
    min=0.0,
    max=1.0,
    description="Progress:",
    bar_style="info",
)
display(float_progress)

# All bar styles
for style in ["success", "info", "warning", "danger", ""]:
    p = widgets.IntProgress(value=60, description=style or "default", bar_style=style)
    display(p)


# ============================================================
# 6. BOOLEAN / VALIDATION WIDGETS
# ============================================================
section("Boolean & Validation")

# Valid
valid_ok = widgets.Valid(value=True, description="Check passed")
display(valid_ok)

valid_fail = widgets.Valid(value=False, description="Check failed")
display(valid_fail)

# Checkbox (already supported)
cb = widgets.Checkbox(value=True, description="Agree to terms")
display(cb)

# ToggleButton (already supported)
toggle = widgets.ToggleButton(value=False, description="Dark Mode")
display(toggle)


# ============================================================
# 7. FILE UPLOAD (UI only - transfer deferred)
# ============================================================
section("File Upload (UI stub)")

upload = widgets.FileUpload(
    accept=".txt,.csv",
    multiple=True,
    description="Upload files",
)
display(upload)
display(HTML("<em>Note: File selection works, but file transfer to kernel requires backend support.</em>"))


# ============================================================
# 8. PLAY (Animation Controller)
# ============================================================
section("Play / Animation")

play = widgets.Play(
    value=0,
    min=0,
    max=100,
    step=1,
    interval=200,
    description="Animate:",
)
slider_linked = widgets.IntSlider(description="Value:")

# Link play to slider so the slider animates
widgets.jslink((play, "value"), (slider_linked, "value"))

display(widgets.HBox([play, slider_linked]))


# ============================================================
# 9. CONTAINER WIDGETS
# ============================================================
section("Container Widgets")

# Tab
tab = widgets.Tab()
tab.children = [
    widgets.IntSlider(description="Tab 1 slider:"),
    widgets.Text(value="Hello from Tab 2", description="Tab 2 text:"),
    widgets.ColorPicker(value="#00ff00", description="Tab 3 color:"),
]
tab.set_title(0, "Slider")
tab.set_title(1, "Text")
tab.set_title(2, "Color")
display(tab)

# Accordion
accordion = widgets.Accordion()
accordion.children = [
    widgets.HTML(value="<b>This is section one</b> with <em>HTML</em> content."),
    widgets.IntProgress(value=42, description="Progress:"),
    widgets.RadioButtons(options=["A", "B", "C"], description="Pick:"),
]
accordion.set_title(0, "HTML Content")
accordion.set_title(1, "Progress Bar")
accordion.set_title(2, "Radio Buttons")
display(accordion)

# Stack (programmatic - no visible tabs)
stack = widgets.Stack(
    children=[
        widgets.HTML(value="<h4>Page 1</h4><p>First page content</p>"),
        widgets.HTML(value="<h4>Page 2</h4><p>Second page content</p>"),
        widgets.HTML(value="<h4>Page 3</h4><p>Third page content</p>"),
    ],
    selected_index=0,
)
stack_dropdown = widgets.Dropdown(options=[("Page 1", 0), ("Page 2", 1), ("Page 3", 2)], description="Show:")
widgets.jslink((stack_dropdown, "index"), (stack, "selected_index"))
display(stack_dropdown)
display(stack)

# VBox / HBox (already supported - nested example)
display(HTML("<b>Nested VBox/HBox:</b>"))
nested = widgets.VBox([
    widgets.HBox([
        widgets.IntSlider(description="A:"),
        widgets.IntSlider(description="B:"),
    ]),
    widgets.HBox([
        widgets.Button(description="OK"),
        widgets.Button(description="Cancel"),
    ]),
])
display(nested)


# ============================================================
# 10. IMAGE / VIDEO / AUDIO (require binary buffer support)
# ============================================================
section("Media Widgets")

# Image - generate a small PNG in memory
try:
    import io
    from PIL import Image as PILImage

    img = PILImage.new("RGB", (200, 100), color=(30, 144, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    image_widget = widgets.Image(value=buf.getvalue(), format="png", width=200, height=100)
    display(image_widget)
except ImportError:
    display(HTML("<em>PIL not available - skipping Image widget test. Install Pillow to test.</em>"))

# Audio - generate a short sine wave WAV
try:
    import struct
    import math

    sample_rate = 8000
    duration = 0.5  # seconds
    freq = 440  # Hz (A4)
    n_samples = int(sample_rate * duration)
    samples = [int(32767 * math.sin(2 * math.pi * freq * t / sample_rate)) for t in range(n_samples)]

    wav_buf = io.BytesIO()
    # WAV header
    data_size = n_samples * 2
    wav_buf.write(b"RIFF")
    wav_buf.write(struct.pack("<I", 36 + data_size))
    wav_buf.write(b"WAVE")
    wav_buf.write(b"fmt ")
    wav_buf.write(struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16))
    wav_buf.write(b"data")
    wav_buf.write(struct.pack("<I", data_size))
    for s in samples:
        wav_buf.write(struct.pack("<h", s))

    audio_widget = widgets.Audio(value=wav_buf.getvalue(), format="wav", autoplay=False)
    display(audio_widget)
except Exception as e:
    display(HTML(f"<em>Audio generation failed: {e}</em>"))

display(HTML("<em>Video widget works the same way - pass video bytes with format='mp4'.</em>"))


# ============================================================
# 11. INTERACTIVE CALLBACK TEST
# ============================================================
section("Interactive Callback Test")

output = widgets.Output()

slider_test = widgets.IntSlider(value=50, min=0, max=100, description="Slide me:")
radio_test = widgets.RadioButtons(options=["A", "B", "C"], description="Pick:")
color_test = widgets.ColorPicker(value="#ff0000", description="Color:")

def on_change(change):
    with output:
        output.clear_output()
        print(f"Slider: {slider_test.value}")
        print(f"Radio:  {radio_test.value}")
        print(f"Color:  {color_test.value}")

slider_test.observe(on_change, names="value")
radio_test.observe(on_change, names="value")
color_test.observe(on_change, names="value")

display(widgets.VBox([slider_test, radio_test, color_test, output]))

print("\nAll widget tests rendered!")
```

### ipywidgets interactivity

Run the first cell and that creates a slider. 

```jupyter
from IPython.display import HTML, display
import ipywidgets as widgets
my_slider = widgets.IntSlider(
    value=7, min=0, max=10, step=1,
    description='Slider A:',
    disabled=False,
    continuous_update=False,
    orientation='horizontal',
    readout=True, readout_format='d'
)
print("Defined a slider variable")
```

Next, run this cell to display it. You can interact with the widget.

```jupyter
display(HTML("<b>Here is a widget!</b>"))
display(widgets.VBox([my_slider]))
```

And now you can reference the same widget in yet another cell, and when you move one, the other moves because user events move from browser to Tzara to Jupyter and then events move back to Tzara and on to browser for broadcasting.

```jupyter
display(HTML("<b>They're the same!</b>"))
display(widgets.VBox([my_slider]))
```

### output widgets
```jupyter
from IPython.display import HTML, display
import ipywidgets as widgets

btn = widgets.Button(description="Click me")
out = widgets.Output()
def on_click(b):
    print("hello world")
    display(HTML("<b>hello world</b>"))
    with out:
        b.description = "Clicked"
        print("Clicked!")
        display(HTML("This work?"))
btn.on_click(on_click)
display(btn, out)
```


## Anywidgets

If you need something not in ipywidgets, then anywidgets is available. 

See <https://anywidget.dev/> for details and examples. These are demos from their website.  There is a little bit of a learning curve, but that's always the case with something powerful. 


```jupyter 
# should already be available
pip install ipywidgets
```

```jupyter
import anywidget
import traitlets

class CounterWidget(anywidget.AnyWidget):
    _esm = """
    export function render({ model, el }) {
        let count = () => model.get("value");
        let btn = document.createElement("button");
        btn.style.padding = "10px 20px";
        btn.style.fontSize = "16px";
        btn.style.cursor = "pointer";
        btn.innerHTML = "count is " + count();
        btn.addEventListener("click", () => {
            model.set("value", count() + 1);
            model.save_changes();
        });
        model.on("change:value", () => {
            btn.innerHTML = "count is " + count();
        });
        el.appendChild(btn);
    }
    """
    value = traitlets.Int(0).tag(sync=True)

CounterWidget()
```

```jupyter
import anywidget
import traitlets

class StyledWidget(anywidget.AnyWidget):
    _esm = """
    export function render({ model, el }) {
        let div = document.createElement("div");
        div.className = "styled-anywidget";
        div.textContent = "Hello from anywidget! Name: " + model.get("name");
        model.on("change:name", () => {
            div.textContent = "Hello from anywidget! Name: " + model.get("name");
        });
        el.appendChild(div);
    }
    """
    _css = """
    .styled-anywidget {
        background: linear-gradient(135deg, #667eea, #764ba2);
        color: white;
        padding: 20px;
        border-radius: 10px;
        font-size: 18px;
        font-weight: bold;
    }
    """
    name = traitlets.Unicode("World").tag(sync=True)

StyledWidget(name="Tzara")
```



## tqdm

Need a progress bar? 

```jupyter
from tqdm.notebook import tqdm
from time import sleep

# Loop with a descriptive label
for i in tqdm(range(100), desc="Processing Data"):
    sleep(0.01)
```


# Plots

There are various plotting libraries that are supported, with varying levels of jankyness. 

## Plotly

This is a plotly example, <https://plotly.com/python/> has more examples to try.  This implementation requires a plotly javascript file in the page.  All the data is being sent to the browser (as opposed to an image composed server side matplotlib style), and the plotly javascript library does all the heavy lifting in the client side web browser. 

This is, admittedly, a potential point of failure. A possible option is to save the plotly library from the content distribution network and just serve it locally.

The performance is snappy, and it can make for an interesting dashboard.

```jupyter
import plotly.express as px
import numpy as np
xs = np.linspace(-3 * np.pi, 3 * np.pi, 1000)
ys = np.cos(xs) + 0.333 * np.cos(3 * xs) + 0.2 * np.cos(5 * xs)
px.scatter(x=xs, y=ys).show()
```

## Matplotlib Static 

```jupyter
import matplotlib.pyplot as plt

xs = range(-10, 10)
ys = [x * x for x in xs]

plt.figure()
plt.plot(list(xs), ys, marker="o")
plt.title("y = x * x")
plt.grid(True)
plt.show()
```

## Matplotlib Interactive

This one might be a little buggy.  Matplotlib is regenerating a PNG image on each redraw, and that's contributing to this being a bit choppy, especially when compared to the plotly.

```jupyter
%matplotlib widget
import matplotlib.pyplot as plt
import numpy as np
x = np.linspace(0, 10, 100)
fig, ax = plt.subplots()
ax.plot(x, np.sin(x))
plt.show()
```

## mpld3

Ok, this one is very buggy.  It needs a pinned copy of the library's javascript served locally, because upstream updates kept breaking it.  Prefer plotly if you want an interactive plot. 

```jupyter
import matplotlib.pyplot as plt
import mpld3
import numpy as np
x = np.linspace(0, 10, 100)
y = np.cos(x) + 0.2 * np.sin(x*5)
fig, ax = plt.subplots()
ax.plot(x,y)
mpld3.display(fig)
```

# File upload

Note, using this widget uploads to the jupyter container, not your vault. 

I haven't found a need for this, but it's here for completeness. 

Run this cell, then pick a file with the widget it displays.

```jupyter
from ipywidgets import FileUpload
u = FileUpload()
u
```

Once you've picked one, the name, size, type and content are readable from the next cell.

```jupyter
print(u.value[0]['name'], u.value[0]['size'], u.value[0]['type'])
print(bytes(u.value[0]['content'])[:64])
```

Pass `multiple=True` to accept several files at once; `u.value` then holds one entry each.

## Related

* [jupyter](../jupyter.md) 
    * [jupyter examples](jupyter-examples.md)
    * [jupyter technical details](jupyter-technical-details.md)
- [markdown syntax](../markdown-syntax.md)
