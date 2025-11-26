# Niconico Provider Tests

Test suite for the Niconico music provider in Music Assistant.

## Fixtures

Test fixtures are JSON snapshots of Niconico API responses used for testing converters and business logic.

### Updating Fixtures

Fixtures are generated using a dedicated tool repository:

**[music-assistant-nicovideo-fixtures](https://github.com/Shi-553/music-assistant-nicovideo-fixtures)**

To update fixtures:

1. Clone the fixtures repository (if not already cloned)
2. Follow setup instructions in that repository
3. Generate new fixtures with your test account: `python scripts/main.py`
4. Copy generated fixtures `cp -r /path/to/music_assistant_nicovideo_fixtures/fixture_data tests/providers/nicovideo/`

**Important:** Always use a dedicated test account, never your personal account!

## Running Tests

```bash
# Run all nicovideo provider tests
pytest tests/providers/nicovideo/

# Run specific test file
pytest tests/providers/nicovideo/test_converters.py

# Run with coverage
pytest --cov=music_assistant.providers.nicovideo tests/providers/nicovideo/
```

## Test Structure

```
tests/providers/nicovideo/
├── fixture_data/         # Fixture data from generator repository
│   ├── fixtures/        # Static JSON fixtures (API responses)
│   ├── fixture_type_mappings.py  # Auto-generated type mappings
│   └── shared_types.py  # Custom fixture types
├── fixtures/            # Fixture loading utilities
├── __snapshots__/       # Generated snapshots for comparison
└── test_*.py           # Test files
```
