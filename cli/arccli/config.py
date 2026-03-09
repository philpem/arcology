"""
Configuration loading for Arcology CLI.

Three-tier precedence (highest wins):
1. CLI flags: --server, --api-key
2. Environment variables: ARCOLOGY_URL, ARCOLOGY_API_KEY
3. Config file: ~/.config/arcology/config.ini
"""

import configparser
import os
import sys
from pathlib import Path


CONFIG_DIR = Path.home() / '.config' / 'arcology'
CONFIG_FILE = CONFIG_DIR / 'config.ini'


def get_config(args):
	"""
	Build configuration from CLI args, environment, and config file.

	Returns a dict with 'url' and 'api_key' keys.
	"""
	# Start with config file defaults
	file_config = _load_config_file(getattr(args, 'profile', 'default'))

	# Layer: config file < env vars < CLI flags
	url = (
		getattr(args, 'server', None)
		or os.environ.get('ARCOLOGY_URL')
		or file_config.get('url')
	)
	api_key = (
		getattr(args, 'api_key', None)
		or os.environ.get('ARCOLOGY_API_KEY')
		or file_config.get('api_key')
	)

	if not url:
		print("Error: No server URL configured.", file=sys.stderr)
		print("Set via --server, ARCOLOGY_URL env var, or 'arco configure'.", file=sys.stderr)
		sys.exit(1)

	if not api_key:
		print("Error: No API key configured.", file=sys.stderr)
		print("Set via --api-key, ARCOLOGY_API_KEY env var, or 'arco configure'.", file=sys.stderr)
		sys.exit(1)

	return {'url': url.rstrip('/'), 'api_key': api_key}


def _load_config_file(profile='default'):
	"""Load configuration from config file."""
	if not CONFIG_FILE.exists():
		return {}

	config = configparser.ConfigParser()
	config.read(CONFIG_FILE)

	if profile not in config:
		if profile != 'default':
			print(f"Warning: profile '{profile}' not found in config file.", file=sys.stderr)
		return {}

	return dict(config[profile])


def create_config(url, api_key, profile='default'):
	"""Create or update config file interactively."""
	CONFIG_DIR.mkdir(parents=True, exist_ok=True)

	config = configparser.ConfigParser()
	if CONFIG_FILE.exists():
		config.read(CONFIG_FILE)

	if profile not in config:
		config[profile] = {}

	config[profile]['url'] = url
	config[profile]['api_key'] = api_key

	with open(CONFIG_FILE, 'w') as f:
		config.write(f)

	# Restrict permissions to owner only
	CONFIG_FILE.chmod(0o600)

	print(f"Configuration saved to {CONFIG_FILE}")

# vim: ts=4 sw=4 noet
