# Demo Notebooks

This directory is meant to contain Jupyter notebook demos for your package.

## Getting Started

Create tutorial notebooks in this directory to demonstrate how to use your package:

1. **Basic Tutorial** (`01-basic.ipynb`): Introduction and basic usage
2. **Intermediate Tutorial** (`02-intermediate.ipynb`): More advanced features
3. **Topic-specific demos** in `topics/` subdirectory

## Creating Notebooks

```bash
# Create a new notebook
jupyter notebook demos/01-basic.ipynb

# Or use JupyterLab
jupyter lab demos/01-basic.ipynb
```

## Testing Notebooks

After creating or modifying notebooks, test them with:

```bash
./scripts/test_notebooks.sh
```

This ensures all notebooks execute without errors.

## Adding to Documentation

Update `mkdocs.yml` to include your notebooks in the navigation:

```yaml
nav:
  - Home: index.md
  - Tutorials:
    - Basic: demos/01-basic.ipynb
    - Intermediate: demos/02-intermediate.ipynb
  - Topics:
    - Your Topic: demos/topics/your-topic.ipynb
  - API: reference.md
```

## Example Notebook Structure

```python
# Cell 1: Imports
import your_package

# Cell 2: Basic usage
result = your_package.some_function()
print(result)

# Cell 3: More examples
...
```
