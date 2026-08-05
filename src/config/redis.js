const redis = require('redis');
const logger = require('./logger');

let redisClient = null;

/**
 * Initialize Redis connection.
 *
 * Two ways to configure, checked in order:
 *   1. REDIS_URL  — a full connection string, e.g. Upstash's
 *      rediss://default:<token>@host:6379  (rediss = TLS on).
 *      This is the production path (Render env var).
 *   2. REDIS_HOST / REDIS_PORT / REDIS_PASSWORD — discrete vars,
 *      the local-dev fallback (plain redis://, no TLS).
 *
 * Gated on REDIS_ENABLED=true so the gateway can run without Redis
 * (sessions/caching simply disabled) — nothing here is load-bearing
 * for pure-P2P alpha.
 */
const connectRedis = async () => {

    if (process.env.REDIS_ENABLED !== 'true') {
        logger.info('Redis disabled (REDIS_ENABLED != true) — skipping connection');
        return null;
    }

    try {
        // ── Build the client from REDIS_URL if given, else discrete vars ──
        let clientOptions;

        if (process.env.REDIS_URL) {
            // Upstash: rediss:// already implies TLS. The redis client reads
            // the scheme and enables TLS automatically for rediss://, but we
            // set socket.tls explicitly so it's unambiguous.
            const useTls = process.env.REDIS_URL.startsWith('rediss://');
            clientOptions = {
                url: process.env.REDIS_URL,
                socket: {
                    tls: useTls,
                    reconnectStrategy: (retries) => {
                        if (retries > 5) {
                            logger.error('Redis reconnection attempts exceeded, giving up');
                            return new Error('Redis connection failed after 5 retries');
                        }
                        return Math.min(retries * 50, 500);
                    }
                }
            };
            logger.info('Redis: connecting via REDIS_URL' + (useTls ? ' (TLS)' : ''));
        } else {
            const host = process.env.REDIS_HOST || 'localhost';
            const port = parseInt(process.env.REDIS_PORT) || 6379;
            const password = process.env.REDIS_PASSWORD;
            const db = parseInt(process.env.REDIS_DB) || 0;
            clientOptions = {
                url: `redis://${password ? `:${password}@` : ''}${host}:${port}/${db}`,
                socket: {
                    reconnectStrategy: (retries) => {
                        if (retries > 5) {
                            logger.error('Redis reconnection attempts exceeded, giving up');
                            return new Error('Redis connection failed after 5 retries');
                        }
                        return Math.min(retries * 50, 500);
                    }
                }
            };
            logger.info(`Redis: connecting via discrete vars (${host}:${port})`);
        }

        redisClient = redis.createClient(clientOptions);

        // Event handlers
        redisClient.on('connect', () => {
            logger.info('Redis connection established');
        });

        redisClient.on('ready', () => {
            logger.info('Redis client ready');
        });

        redisClient.on('error', (err) => {
            logger.error('Redis error:', err);
        });

        redisClient.on('end', () => {
            logger.warn('Redis connection closed');
        });

        redisClient.on('reconnecting', () => {
            logger.info('Redis reconnecting...');
        });

        // Connect to Redis
        await redisClient.connect();

        // Test connection
        await redisClient.ping();

        logger.info('Redis connected successfully');

    } catch (error) {
        logger.error('Redis connection failed:', error);

        // Don't crash the server, but log the error.
        // The app can still function without Redis, albeit with reduced performance.
        logger.warn('Continuing without Redis - sessions and caching will be disabled');

        return null;
    }
};

/**
 * Get Redis client instance
 */
const getRedisClient = () => {
    return redisClient;
};

/**
 * Redis service wrapper with error handling
 */
class RedisService {
    static async get(key) {
        try {
            if (!redisClient || !redisClient.isReady) {
                logger.warn('Redis not available, skipping get operation');
                return null;
            }

            const value = await redisClient.get(key);
            return value ? JSON.parse(value) : null;
        } catch (error) {
            logger.error('Redis GET error:', { key, error: error.message });
            return null;
        }
    }

    static async set(key, value, expireSeconds = null) {
        try {
            if (!redisClient || !redisClient.isReady) {
                logger.warn('Redis not available, skipping set operation');
                return false;
            }

            const stringValue = JSON.stringify(value);

            if (expireSeconds) {
                await redisClient.setEx(key, expireSeconds, stringValue);
            } else {
                await redisClient.set(key, stringValue);
            }

            return true;
        } catch (error) {
            logger.error('Redis SET error:', { key, error: error.message });
            return false;
        }
    }

    static async del(key) {
        try {
            if (!redisClient || !redisClient.isReady) {
                logger.warn('Redis not available, skipping del operation');
                return false;
            }

            const result = await redisClient.del(key);
            return result > 0;
        } catch (error) {
            logger.error('Redis DEL error:', { key, error: error.message });
            return false;
        }
    }

    static async exists(key) {
        try {
            if (!redisClient || !redisClient.isReady) {
                return false;
            }

            const result = await redisClient.exists(key);
            return result > 0;
        } catch (error) {
            logger.error('Redis EXISTS error:', { key, error: error.message });
            return false;
        }
    }

    static async increment(key, amount = 1, expireSeconds = null) {
        try {
            if (!redisClient || !redisClient.isReady) {
                logger.warn('Redis not available, skipping increment operation');
                return 0;
            }

            const result = await redisClient.incrBy(key, amount);

            if (expireSeconds && result === amount) {
                // This is a new key, set expiration
                await redisClient.expire(key, expireSeconds);
            }

            return result;
        } catch (error) {
            logger.error('Redis INCREMENT error:', { key, error: error.message });
            return 0;
        }
    }

    static async invalidatePattern(pattern) {
        try {
            if (!redisClient || !redisClient.isReady) {
                logger.warn('Redis not available, skipping pattern invalidation');
                return 0;
            }

            const keys = await redisClient.keys(pattern);

            if (keys.length > 0) {
                const result = await redisClient.del(keys);
                logger.info('Redis pattern invalidated', { pattern, keysDeleted: result });
                return result;
            }

            return 0;
        } catch (error) {
            logger.error('Redis pattern invalidation error:', { pattern, error: error.message });
            return 0;
        }
    }

    static async healthCheck() {
        try {
            if (!redisClient || !redisClient.isReady) {
                return {
                    status: 'disconnected',
                    error: 'Redis client not ready'
                };
            }

            const startTime = Date.now();
            await redisClient.ping();
            const responseTime = Date.now() - startTime;

            const info = await redisClient.info('memory');

            return {
                status: 'connected',
                responseTime: `${responseTime}ms`,
                memory: this.parseRedisInfo(info)
            };
        } catch (error) {
            return {
                status: 'error',
                error: error.message
            };
        }
    }

    static parseRedisInfo(info) {
        const lines = info.split('\r\n');
        const result = {};

        for (const line of lines) {
            if (line.includes(':')) {
                const [key, value] = line.split(':');
                result[key] = value;
            }
        }

        return {
            used_memory_human: result.used_memory_human,
            used_memory_peak_human: result.used_memory_peak_human,
            used_memory_rss_human: result.used_memory_rss_human
        };
    }

    static async flushAll() {
        try {
            if (!redisClient || !redisClient.isReady) {
                return false;
            }

            await redisClient.flushAll();
            return true;
        } catch (error) {
            logger.error('Redis FLUSHALL error:', error);
            return false;
        }
    }
}

// Handle graceful shutdown
process.on('SIGTERM', async () => {
    if (redisClient) {
        await redisClient.quit();
    }
});

process.on('SIGINT', async () => {
    if (redisClient) {
        await redisClient.quit();
    }
});

module.exports = {
    connectRedis,
    redisClient: getRedisClient,
    RedisService
};