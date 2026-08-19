# Runnable Jupyter Examples

Tzara can execute code cells embedded right in a page. Fence a block with the `jupyter` language and it becomes a live cell with a **Run** button; its output renders inline beneath it. All cells on a page share one kernel, so later cells see variables defined earlier.


> [!warning]
>  Executable cells run in a Jupyter kernel that has access to your vaults. Only run code you understand or wrote yourself.


## A first cell

```jupyter
message = "Hello World!"
print(message)
```

## Cells share state

The variable above is still in scope here:

```jupyter
print(message.upper())
```

## Computation

```jupyter
import math
radius = 5
area = math.pi * radius ** 2
print(f"A circle of radius {radius} has area {area:.2f}")
```

## Plots render inline

Matplotlib (and a few other libraries) are installed into the base Jupyter image.  You can change or edit them as needed. See `Dockerfile.jupyter`. 

```jupyter
import matplotlib.pyplot as plt

xs = range(-4, 4)
ys = [x * x for x in xs]

plt.figure()
plt.plot(list(xs), ys, marker="o")
plt.title("y = x * x")
plt.grid(True)
plt.show()
```

## Tables and data
```jupyter
import pandas as pd

# Example data
data = {
    "Student": ["Alice", "Bob", "Charlie", "Diana", "Ethan"],
    "Score": [88, 92, 79, 95, 100]
}
df = pd.DataFrame(data)

df
```

```jupyter
# Basic statistics
print("\nSummary statistics:")
df["Score"].describe()
```

## Jupytext
Want to make plain `python` blocks executable too? Set `EXECUTABLE_CODE_LANGUAGES=jupyter,python` in your environment.  See [configurations](../configurations.md).

## Related

* [jupyter](../jupyter.md) 
    * [jupyter more examples](jupyter-more-examples.md)
    * [jupyter technical details](jupyter-technical-details.md)
