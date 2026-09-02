import sys, pathlib
p = pathlib.Path(sys.argv[1])
c = p.read_text('utf-8')
c = c.replace('self._check_api_enabled(hass)', 'self._check_api_enabled(request)')
p.write_text(c, 'utf-8')
print('OK')
