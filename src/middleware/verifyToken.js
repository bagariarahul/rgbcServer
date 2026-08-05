const jwt = require('jsonwebtoken');
const fs = require('fs');
const path = require('path');
const logger = require('../config/logger');

/**
 * Sprint 3 — RS256 JWT Verification Middleware
 *
 * BREAKING CHANGES from Sprint 2:
 *   1. HS256 → RS256: Tokens are now verified with the RSA public key,
 *      not the symmetric JWT_SECRET. The public key can be freely
 *      distributed — compromise does NOT enable token forging.
 *   2. M2M_API_KEY REMOVED: The Python Master now authenticates via
 *      Google OAuth and receives its own JWT. The X-API-Key header
 *      path is eliminated entirely.
 *
 * Key file expected: src/keys/public.pem
 */



//For Local
// const KEYS_DIR = path.resolve(__dirname, '..', 'keys');

// let PUBLIC_KEY = null;
// try {
//     PUBLIC_KEY = fs.readFileSync(path.join(KEYS_DIR, 'public.pem'), 'utf8');
//     logger.info('✅ RS256 public key loaded from src/keys/public.pem');
// } catch (e) {
//     logger.error(
//         '❌ RS256 public key not found at src/keys/public.pem\n' +
//         '   Generate it with:\n' +
//         '     cd src/keys && openssl genrsa -out private.pem 2048\n' +
//         '     openssl rsa -in private.pem -pubout -out public.pem\n'
//     );
// }

const KEYS_DIR = path.resolve(__dirname, '..', 'keys');

let PUBLIC_KEY = null;
try {
    if (process.env.JWT_PUBLIC_KEY_B64) {
        PUBLIC_KEY = Buffer.from(process.env.JWT_PUBLIC_KEY_B64, 'base64').toString('utf8');
        logger.info('✅ RS256 public key loaded from JWT_PUBLIC_KEY_B64 env var');
    } else {
        PUBLIC_KEY = fs.readFileSync(path.join(KEYS_DIR, 'public.pem'), 'utf8');
        logger.info('✅ RS256 public key loaded from src/keys/public.pem');
    }
} catch (e) {
    logger.error(
        '❌ RS256 public key not available.\n' +
        '   Set JWT_PUBLIC_KEY_B64 (base64 of public.pem) in the environment,\n' +
        '   or place src/keys/public.pem for local dev.'
    );
}
const verifyToken = (req, res, next) => {
    try {
        const authHeader = req.headers.authorization;

        if (!authHeader || !authHeader.startsWith('Bearer ')) {
            return res.status(401).json({
                error: 'Authentication required',
                message: 'Missing or invalid Authorization header. Provide: Authorization: Bearer <jwt>',
                code: 'AUTH_TOKEN_MISSING'
            });
        }

        if (!PUBLIC_KEY) {
            logger.error('RS256 public key not loaded — cannot verify tokens');
            return res.status(500).json({
                error: 'Server configuration error',
                message: 'RS256 public key not found on server',
                code: 'KEY_MISSING'
            });
        }

        const token = authHeader.substring(7);

        // Verify JWT with RSA public key (RS256)
        const decoded = jwt.verify(token, PUBLIC_KEY, {
            algorithms: ['RS256']
        });

        // Attach decoded user info to request
        req.user = {
            id: decoded.userId,
            email: decoded.email,
            sessionId: decoded.sessionId
        };

        next();

    } catch (error) {
        if (error.name === 'TokenExpiredError') {
            return res.status(401).json({
                error: 'Token expired',
                message: 'Your session has expired. Please sign in again.',
                code: 'TOKEN_EXPIRED'
            });
        }

        if (error.name === 'JsonWebTokenError') {
            logger.warn(`Invalid token: ${error.message}`);
            return res.status(401).json({
                error: 'Invalid token',
                message: 'Token verification failed. Ensure you are using a current RS256 token.',
                code: 'INVALID_TOKEN'
            });
        }

        logger.error('Auth middleware error:', error);
        return res.status(500).json({
            error: 'Authentication error',
            message: 'Internal server error during authentication',
            code: 'AUTH_INTERNAL_ERROR'
        });
    }
};

module.exports = verifyToken;