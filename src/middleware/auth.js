const jwt = require('jsonwebtoken');
const bcrypt = require('bcryptjs');

const { User, SyncSession, Device } = require('../config/database');
const { RedisService } = require('../config/redis');
const logger = require('../config/logger');

/**
 * Authentication middleware for protecting API routes
 * Validates JWT tokens and maintains session state
 */
const authMiddleware = async (req, res, next) => {
    try {
        // Extract token from Authorization header
        const authHeader = req.headers.authorization;
        
        if (!authHeader || !authHeader.startsWith('Bearer ')) {
            return res.status(401).json({
                error: 'Authentication required',
                message: 'Missing or invalid authorization header',
                code: 'AUTH_TOKEN_MISSING'
            });
        }

        const token = authHeader.substring(7);

        // Verify JWT token
        let decoded;
        try {
            decoded = jwt.verify(token, process.env.JWT_SECRET);
        } catch (error) {
            logger.audit('invalid_token_attempt', { 
                error: error.message,
                token: token.substring(0, 10) + '...'
            }, req);
            
            return res.status(401).json({
                error: 'Invalid token',
                message: 'Token verification failed',
                code: 'INVALID_TOKEN'
            });
        }

        // Check token structure
        if (!decoded.userId || !decoded.sessionId) {
            logger.audit('malformed_token', { decoded }, req);
            return res.status(401).json({
                error: 'Invalid token',
                message: 'Token is malformed',
                code: 'MALFORMED_TOKEN'
            });
        }

        // Check Redis cache first for performance
        const cacheKey = `user_session:${decoded.userId}:${decoded.sessionId}`;
        let cachedSession = await RedisService.get(cacheKey);
        
        let session;
        if (cachedSession) {
            // Use cached session data but still verify it's active
            session = cachedSession;
            
            // Verify session hasn't been revoked in database (check periodically)
            const shouldVerifyDB = Math.random() < 0.1; // 10% chance to verify with DB
            if (shouldVerifyDB) {
                const dbSession = await SyncSession.findByIdAndUserId(decoded.sessionId, decoded.userId);
                if (!dbSession || !dbSession.isActive()) {
                    // Session revoked, clear cache
                    await RedisService.del(cacheKey);
                    return res.status(401).json({
                        error: 'Session expired',
                        message: 'Session has been revoked or expired',
                        code: 'SESSION_REVOKED'
                    });
                }
            }
        } else {
            // Fetch from database
            const dbSession = await SyncSession.findByIdAndUserId(decoded.sessionId, decoded.userId);
            
            if (!dbSession) {
                logger.audit('session_not_found', { 
                    userId: decoded.userId, 
                    sessionId: decoded.sessionId 
                }, req);
                
                return res.status(401).json({
                    error: 'Session not found',
                    message: 'Session has expired or been revoked',
                    code: 'SESSION_NOT_FOUND'
                });
            }

            if (!dbSession.isActive()) {
                logger.audit('inactive_session_attempt', { 
                    userId: decoded.userId, 
                    sessionId: decoded.sessionId,
                    isRevoked: dbSession.is_revoked,
                    isExpired: dbSession.isExpired()
                }, req);
                
                return res.status(401).json({
                    error: 'Session inactive',
                    message: 'Session has expired or been revoked',
                    code: 'SESSION_INACTIVE'
                });
            }

            // Cache session data
            session = {
                id: dbSession.id,
                user_id: dbSession.user_id,
                device_id: dbSession.device_id,
                user: dbSession.user ? dbSession.user.toPublicJSON() : null,
                device: dbSession.device ? dbSession.device.toPublicJSON() : null,
                expires_at: dbSession.expires_at,
                last_activity: dbSession.last_activity
            };

            // Cache for 5 minutes
            await RedisService.set(cacheKey, session, 300);
        }

        // Verify stored token hash (security measure)
        if (session.verify_token !== false) { // Allow cache to skip verification
            const dbSession = await SyncSession.findByPk(session.id);
            if (dbSession) {
                const isValidToken = await bcrypt.compare(token, dbSession.access_token_hash);
                if (!isValidToken) {
                    logger.audit('token_hash_mismatch', { 
                        userId: decoded.userId, 
                        sessionId: decoded.sessionId 
                    }, req);
                    
                    // Clear cache as it might be compromised
                    await RedisService.del(cacheKey);
                    
                    return res.status(401).json({
                        error: 'Invalid token',
                        message: 'Token verification failed',
                        code: 'TOKEN_HASH_MISMATCH'
                    });
                }
            }
        }

        // Update last activity (throttled to prevent too many DB writes)
        const lastActivityKey = `last_activity:${decoded.sessionId}`;
        const lastUpdate = await RedisService.get(lastActivityKey);
        const now = Date.now();
        
        // Only update every 60 seconds
        if (!lastUpdate || (now - lastUpdate) > 60000) {
            // Update in background, don't wait
            updateSessionActivity(decoded.sessionId, req).catch(error => {
                logger.warn('Failed to update session activity:', error);
            });
            
            // Cache the update time
            await RedisService.set(lastActivityKey, now, 300);
        }

        // Attach user data to request
        req.user = {
            id: session.user_id,
            email: decoded.email,
            sessionId: session.id,
            deviceId: session.device_id,
            ...session.user
        };

        req.device = session.device;
        req.session = {
            id: session.id,
            expires_at: session.expires_at,
            last_activity: session.last_activity
        };

        // Add user context to logger
        req.logContext = {
            userId: req.user.id,
            sessionId: req.session.id,
            deviceId: req.device?.id
        };

        next();

    } catch (error) {
        logger.error('Authentication middleware error:', error);
        
        // Clear potentially corrupted cache
        if (req.body?.sessionId) {
            await RedisService.del(`user_session:*:${req.body.sessionId}`);
        }
        
        return res.status(500).json({
            error: 'Authentication error',
            message: 'An internal server error occurred during authentication',
            code: 'AUTH_INTERNAL_ERROR'
        });
    }
};

/**
 * Background function to update session activity
 */
async function updateSessionActivity(sessionId, req) {
    try {
        const session = await SyncSession.findByPk(sessionId);
        if (session) {
            await session.updateActivity(
                req.ip || req.connection?.remoteAddress,
                req.get('User-Agent')
            );

            // Also update device last seen
            if (session.device_id) {
                const device = await Device.findByPk(session.device_id);
                if (device) {
                    await device.updateLastSeen();
                }
            }
        }
    } catch (error) {
        logger.warn('Failed to update session activity:', error);
    }
}

/**
 * Optional middleware to check if user is admin
 */
const adminMiddleware = (req, res, next) => {
    if (!req.user) {
        return res.status(401).json({
            error: 'Authentication required',
            message: 'This endpoint requires authentication',
            code: 'AUTH_REQUIRED'
        });
    }

    if (!req.user.is_admin) {
        logger.audit('admin_access_denied', { userId: req.user.id }, req);
        return res.status(403).json({
            error: 'Access denied',
            message: 'Admin privileges required',
            code: 'ADMIN_REQUIRED'
        });
    }

    next();
};

/**
 * Middleware to check storage quota before file operations
 */
const storageQuotaMiddleware = async (req, res, next) => {
    try {
        if (!req.user) {
            return res.status(401).json({
                error: 'Authentication required',
                code: 'AUTH_REQUIRED'
            });
        }

        // Get current user with fresh storage data
        const user = await User.findByPk(req.user.id, {
            attributes: ['storage_used', 'storage_quota', 'subscription_type']
        });

        if (!user) {
            return res.status(404).json({
                error: 'User not found',
                code: 'USER_NOT_FOUND'
            });
        }

        // Check if user is approaching or exceeding quota
        const usage = (user.storage_used / user.storage_quota) * 100;
        const fileSize = req.headers['content-length'] ? parseInt(req.headers['content-length']) : 0;
        
        // Check if this upload would exceed quota
        if (user.storage_used + fileSize > user.storage_quota) {
            logger.audit('storage_quota_exceeded', { 
                userId: user.id,
                currentUsage: user.storage_used,
                quota: user.storage_quota,
                attemptedUpload: fileSize
            }, req);
            
            return res.status(413).json({
                error: 'Storage quota exceeded',
                message: `Upload would exceed storage quota. Used: ${user.storage_used}, Quota: ${user.storage_quota}`,
                code: 'STORAGE_QUOTA_EXCEEDED',
                details: {
                    used: user.storage_used,
                    quota: user.storage_quota,
                    remaining: user.storage_quota - user.storage_used,
                    usage_percentage: Math.round(usage)
                }
            });
        }

        // Warn if approaching quota (90%)
        if (usage >= 90) {
            logger.audit('storage_quota_warning', { 
                userId: user.id,
                usage: usage
            }, req);
            
            // Add warning header
            res.setHeader('X-Storage-Warning', 'Approaching storage quota limit');
        }

        // Attach storage info to request
        req.storageInfo = {
            used: user.storage_used,
            quota: user.storage_quota,
            remaining: user.storage_quota - user.storage_used,
            usage_percentage: Math.round(usage)
        };

        next();

    } catch (error) {
        logger.error('Storage quota middleware error:', error);
        return res.status(500).json({
            error: 'Storage check failed',
            message: 'Failed to verify storage quota',
            code: 'STORAGE_CHECK_ERROR'
        });
    }
};

/**
 * Rate limiting middleware for file operations
 */
const fileOperationLimitMiddleware = async (req, res, next) => {
    try {
        const userId = req.user?.id;
        if (!userId) return next();

        const key = `file_ops:${userId}`;
        const windowSize = 60; // 1 minute
        const maxOperations = 100; // Max 100 file operations per minute

        const count = await RedisService.increment(key, 1, windowSize);
        
        if (count > maxOperations) {
            logger.audit('file_operation_rate_limit', { 
                userId,
                count,
                limit: maxOperations
            }, req);
            
            return res.status(429).json({
                error: 'Rate limit exceeded',
                message: `Too many file operations. Limit: ${maxOperations} per minute`,
                code: 'FILE_RATE_LIMIT_EXCEEDED',
                retryAfter: windowSize
            });
        }

        // Add rate limit headers
        res.setHeader('X-RateLimit-Limit', maxOperations);
        res.setHeader('X-RateLimit-Remaining', Math.max(0, maxOperations - count));
        res.setHeader('X-RateLimit-Reset', new Date(Date.now() + windowSize * 1000).toISOString());

        next();

    } catch (error) {
        logger.warn('File operation rate limit middleware error:', error);
        next(); // Continue on error to not break functionality
    }
};

module.exports = {
    authMiddleware,
    adminMiddleware,
    storageQuotaMiddleware,
    fileOperationLimitMiddleware
};