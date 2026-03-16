const jwt = require('jsonwebtoken');
const crypto = require('crypto');
const logger = require('../config/logger');

/**
 * Unified authentication middleware.
 *
 * Supports two authentication methods:
 *
 * 1. JWT Bearer Token (Android app / browser):
 *    Authorization: Bearer <jwt_token>
 *
 * 2. M2M API Key (headless Python/Windows client):
 *    X-API-Key: <api_key>
 *
 * The M2M API key is a long-lived secret stored in the backend .env
 * as M2M_API_KEY. Generate one with: openssl rand -hex 32
 */
const verifyToken = (req, res, next) => {
    try {
        // ── Path 1: Check for M2M API Key header first ──────────────
        const apiKey = req.headers['x-api-key'];
        if (apiKey) {
            const validApiKey = process.env.M2M_API_KEY;

            if (!validApiKey) {
                logger.warn('M2M_API_KEY not configured in .env — rejecting API key auth');
                return res.status(500).json({
                    error: 'Server configuration error',
                    message: 'API key authentication is not configured',
                    code: 'M2M_NOT_CONFIGURED'
                });
            }

            // Constant-time comparison to prevent timing attacks
            try {
                const keyBuffer = Buffer.from(apiKey, 'utf-8');
                const validBuffer = Buffer.from(validApiKey, 'utf-8');

                if (keyBuffer.length === validBuffer.length &&
                    crypto.timingSafeEqual(keyBuffer, validBuffer)
                ) {
                    req.user = {
                        id: 'M2M_CLIENT',
                        email: process.env.ALLOWED_ADMIN_EMAIL || 'admin@system',
                        sessionId: 'M2M_SESSION',
                        authMethod: 'API_KEY'
                    };
                    logger.debug('M2M API key authentication successful');
                    return next();
                }
            } catch (e) {
                // timingSafeEqual throws if lengths differ — that's a mismatch
            }

            logger.warn('Invalid M2M API key presented');
            return res.status(401).json({
                error: 'Invalid API key',
                message: 'The provided API key is not valid',
                code: 'INVALID_API_KEY'
            });
        }

        // ── Path 2: Check for JWT Bearer token ──────────────────────
        const authHeader = req.headers.authorization;

        if (!authHeader || !authHeader.startsWith('Bearer ')) {
            return res.status(401).json({
                error: 'Authentication required',
                message: 'Provide a Bearer token or X-API-Key header',
                code: 'AUTH_TOKEN_MISSING'
            });
        }

        const token = authHeader.substring(7);
        const decoded = jwt.verify(token, process.env.JWT_SECRET);

        req.user = {
            id: decoded.userId,
            email: decoded.email,
            sessionId: decoded.sessionId,
            authMethod: 'JWT'
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
            return res.status(401).json({
                error: 'Invalid token',
                message: 'Token verification failed',
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