import '@testing-library/jest-dom';

// jsdom's test environment does not expose the Web Crypto API globally the
// way a real browser does. `crypto.randomUUID()` is used for every
// human-review submission's `client_event_id` (SessionDetail.jsx); without
// this polyfill it throws "crypto is not defined" and the submit silently
// no-ops instead of posting -- caught while adding SessionDetail.jsx's
// whoami()-based reviewer identity (D13 step 8).
if (typeof global.crypto === 'undefined' || typeof global.crypto.randomUUID !== 'function') {
  // eslint-disable-next-line global-require
  const nodeCrypto = require('crypto');
  global.crypto = nodeCrypto;
}
