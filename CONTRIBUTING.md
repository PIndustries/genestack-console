# Contributing

## Development setup

```bash
# Install deps
pip install -r requirements.txt

# Run dev server
uvicorn app.main:app --reload --app-dir .

# Open http://localhost:8000
```

## Code style

- Black for Python formatting
- Prettier for JS formatting
- Type hints on all public functions

## Testing

```bash
pytest tests/ -v
```

## Submitting changes

1. Fork the repo
2. Create a feature branch
3. Write tests for new functionality
4. Open a PR with description of changes
