# Topics

This directory contains topic-specific notebooks that demonstrate particular use cases or features.

## Adding New Topics

To add a new topic notebook:

1. Create a new `.ipynb` file in this directory (e.g., `my-topic.ipynb`)
2. Add it to the navigation in `mkdocs.yml`:
   ```yaml
   nav:
     - Home: index.md
     - Tutorials:
       - Basic: demos/01-basic.ipynb
       - Intermediate: demos/02-intermediate.ipynb
     - Topics:
       - My Topic: demos/topics/my-topic.ipynb
     - API: reference.md
   ```
3. Run `./scripts/test_notebooks.sh` to ensure it executes without errors
4. Commit and push your changes

## Testing Notebooks

Before committing any notebook changes, always run:

```bash
./scripts/test_notebooks.sh
```

This ensures all notebooks execute without errors and helps maintain quality.
