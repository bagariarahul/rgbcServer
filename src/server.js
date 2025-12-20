// src/server.js
require('dotenv').config();
const express = require('express');
const cors = require('cors');
const helmet = require('helmet');
const morgan = require('morgan');
const compression = require('compression');
const rateLimit = require('express-rate-limit');
const http = require('http');
const fs = require('fs').promises;
const session = require('express-session');

require('dotenv').config();

const logger = require('./config/logger');

class CloudBackupServer {
    constructor() {
        this.app = express();
        this.server = null;
        this.io = null;
        this.PORT = process.env.PORT || 3000;
        this.isShuttingDown = false;
    }

    async start() {
        try {
            logger.info('Starting Cloud Backup Server...');

            // create HTTP server
            this.server = http.createServer(this.app);
            logger.info('HTTP server created');

            // configure middleware & routes
            this.setupBasicMiddleware();
            logger.info('Basic middleware configured');

            this.setupBasicRoutes();
            logger.info('Basic routes configured');

            this.setupErrorHandling();
            logger.info('Error handling configured');

            await this.createDirectories();
            logger.info('Storage directories created');

            // start listening on all interfaces
            await new Promise((resolve, reject) => {
                this.server.listen(this.PORT, '0.0.0.0', (err) => {
                    if (err) return reject(err);
                    console.log(`✅ Server now listening on 0.0.0.0:${this.PORT}`);
                    resolve();
                });
            });

            logger.info(`Cloud Backup Server started (env=${process.env.NODE_ENV || 'development'})`);

            // initialize advanced features (db, redis, routes)
            await this.initializeAdvancedFeatures();

            // ensure routes are mounted (safe to call again)
            this.setupAdvancedRoutes();

            // optional: list registered routes (debug)
            if (process.env.LOG_ROUTES === 'true') {
                try {
                    const routes = [];
                    if (this.app && this.app._router && this.app._router.stack) {
                        this.app._router.stack.forEach(layer => {
                            if (layer.route && layer.route.path) {
                                const methods = Object.keys(layer.route.methods || {}).map(m => m.toUpperCase()).join(',');
                                routes.push(`${methods} ${layer.route.path}`);
                            } else if (layer.name === 'router' && layer.handle && layer.handle.stack) {
                                layer.handle.stack.forEach(r => {
                                    if (r.route && r.route.path) {
                                        const methods = Object.keys(r.route.methods || {}).map(m => m.toUpperCase()).join(',');
                                        const prefixMatch = layer.regexp && layer.regexp.source ? layer.regexp.source : '';
                                        routes.push(`${methods} ${prefixMatch}${r.route.path}`);
                                    }
                                });
                            }
                        });
                    }
                    logger.info('Registered routes (debug)', { routes });
                } catch (e) {
                    logger.warn('Failed to list routes', e.message);
                }
            }

            // final 404 (must be last)
            this.app.use('*', (req, res) => {
                res.status(404).json({
                    error: 'Not Found',
                    message: `Route ${req.method} ${req.originalUrl} not found`,
                    timestamp: new Date().toISOString(),
                    requestId: req.requestId || null
                });
            });

        } catch (error) {
            logger.error('Failed to start server:', error);
            process.exit(1);
        }
    }

    setupBasicMiddleware() {
        // security
        this.app.use(helmet({
            crossOriginEmbedderPolicy: false,
            contentSecurityPolicy: false
        }));

        // cors
        this.app.use(cors({ origin: true, credentials: true }));

        // request body parsing
        this.app.use(compression());
        this.app.use(express.json({ limit: process.env.MAX_JSON_BODY || '10mb' }));
        this.app.use(express.urlencoded({ extended: true, limit: process.env.MAX_JSON_BODY || '10mb' }));

        // rate limiter
        const limiter = rateLimit({
            windowMs: parseInt(process.env.RATE_LIMIT_WINDOW_MS, 10) || 15 * 60 * 1000,
            max: parseInt(process.env.RATE_LIMIT_MAX_REQUESTS, 10) || 100,
            message: { error: 'Too many requests', message: 'Rate limit exceeded. Please try again later.' }
        });
        this.app.use(limiter);

        // session middleware - REQUIRED for passport.session()
        // Dev note: in-memory store is fine for local development but NOT for production.
        const sessionSecret = process.env.SESSION_SECRET || 'dev_session_secret_change_me';
        const sessionOptions = {
            secret: sessionSecret,
            resave: false,
            saveUninitialized: false,
            cookie: {
                secure: process.env.NODE_ENV === 'production', // set true when HTTPS
                httpOnly: true,
                maxAge: 7 * 24 * 60 * 60 * 1000
            }
        };

        // Optional Redis-backed session store (recommended for production):
        // To enable: install connect-redis and ioredis and set REDIS_SESSION=true in .env
        if (process.env.REDIS_SESSION === 'true') {
            try {
                // Lazy require to keep local dev simple
                const connectRedis = require('connect-redis');
                const IORedis = require('ioredis');
                const RedisStore = connectRedis(session);
                const redisClient = new IORedis({
                    host: process.env.REDIS_HOST || '127.0.0.1',
                    port: parseInt(process.env.REDIS_PORT || '6379', 10),
                    password: process.env.REDIS_PASSWORD || undefined,
                    db: parseInt(process.env.REDIS_DB || '0', 10)
                });
                sessionOptions.store = new RedisStore({ client: redisClient });
                logger.info('Using Redis session store');
            } catch (e) {
                logger.warn('REDIS_SESSION requested but connect-redis/ioredis not installed. Falling back to memory store.');
            }
        }

        this.app.use(session(sessionOptions));

        // request id
        this.app.use((req, res, next) => {
            try {
                req.requestId = require('crypto').randomUUID();
                res.setHeader('X-Request-ID', req.requestId);
            } catch (e) { /* ignore */ }
            next();
        });

        // morgan -> logger
        if (process.env.NODE_ENV !== 'test') {
            this.app.use(morgan('combined', { stream: { write: (msg) => logger.info(msg.trim()) } }));
        }

        // optional debug-request middleware (enable with DEBUG_REQUESTS=true)
        if (process.env.DEBUG_REQUESTS === 'true') {
            this.app.use((req, res, next) => {
                try {
                    if (req.headers['content-type'] && req.headers['content-type'].includes('application/json')) {
                        let data = '';
                        req.on('data', c => { data += c.toString(); });
                        req.on('end', () => {
                            const preview = data.length > 1000 ? data.slice(0, 1000) + '...[truncated]' : data;
                            logger.info('INCOMING REQUEST', { method: req.method, url: req.originalUrl, bodyPreview: preview, remoteAddress: req.socket && req.socket.remoteAddress });
                            try { if (preview) req.body = JSON.parse(preview); } catch (e) { }
                            next();
                        });
                    } else {
                        logger.info('INCOMING REQUEST', { method: req.method, url: req.originalUrl, remoteAddress: req.socket && req.socket.remoteAddress });
                        next();
                    }
                } catch (e) {
                    logger.warn('Request debug middleware error', e.message);
                    next();
                }
            });
        }
    }

    setupBasicRoutes() {
        this.app.get('/health', (req, res) => {
            const health = {
                status: 'healthy',
                timestamp: new Date().toISOString(),
                uptime: process.uptime(),
                version: '1.0.0',
                environment: process.env.NODE_ENV || 'development',
                services: { http: 'active', database: process.env.DB_SYNC === 'true' ? 'unknown' : 'not_connected', redis: 'not_connected', storage: 'available' },
                system: { memory: process.memoryUsage(), cpu: process.cpuUsage() }
            };
            res.json(health);
        });

        this.app.get('/', (req, res) => {
            res.json({ name: 'Cloud Backup Server', version: '1.0.0', status: 'running', mode: 'minimal', endpoints: { health: '/health', api: '/api/*' } });
        });

        if (process.env.DEBUG_ROUTES === 'true') {
            this.app.get('/api/auth/test-direct', (req, res) => res.json({ ok: true, source: 'direct-server-route' }));
        }
    }

    setupErrorHandling() {
        // Express error handler
        this.app.use((err, req, res, next) => {
            logger.error('Global error handler:', { error: err && err.message, stack: err && err.stack, url: req && req.originalUrl, method: req && req.method, requestId: req && req.requestId });
            res.status(500).json({ error: 'Internal Server Error', message: 'Something went wrong on our end', requestId: req && req.requestId, timestamp: new Date().toISOString() });
        });

        process.on('uncaughtException', (error) => { logger.error('Uncaught Exception:', error); this.shutdown('UNCAUGHT_EXCEPTION'); });
        process.on('unhandledRejection', (reason, promise) => { logger.error('Unhandled Rejection:', { reason, promise }); this.shutdown('UNHANDLED_REJECTION'); });
        process.on('SIGTERM', () => this.shutdown('SIGTERM'));
        process.on('SIGINT', () => this.shutdown('SIGINT'));
    }

    async createDirectories() {
        const dirs = [process.env.UPLOAD_PATH || './storage/uploads', process.env.TEMP_PATH || './storage/temp', process.env.PREVIEW_PATH || './storage/previews', './logs'];
        for (const dir of dirs) {
            try { await fs.mkdir(dir, { recursive: true }); logger.debug(`Directory ensured: ${dir}`); } catch (e) { logger.warn(`Could not create directory ${dir}: ${e.message}`); }
        }
    }

    async initializeAdvancedFeatures() {
        try {
            logger.info('Attempting to initialize advanced features...');
            // DB
            try {
                const dbModule = require('./config/database');
                if (dbModule && typeof dbModule.connectDB === 'function') {
                    await dbModule.connectDB();
                    logger.info('Database connected successfully');
                } else {
                    logger.warn('Database module does not export connectDB() — skipping DB connect');
                }
            } catch (e) {
                logger.warn('Database connection failed, continuing without database:', e.message || e);
            }

            // Redis
            try {
                const redisModule = require('./config/redis');
                if (redisModule && typeof redisModule.connectRedis === 'function') {
                    await redisModule.connectRedis();
                    logger.info('Redis connected successfully');
                } else {
                    logger.warn('Redis module does not export connectRedis() — skipping Redis connect');
                }
            } catch (e) {
                logger.warn('Redis connection failed, continuing without Redis:', e.message || e);
            }

            // Setup advanced routes
            try { this.setupAdvancedRoutes(); logger.info('Advanced routes configured'); } catch (e) { logger.warn('Advanced routes setup failed:', e.message || e); }

            logger.info('Advanced features initialization completed');
        } catch (e) {
            logger.error('Advanced features initialization failed:', e);
        }
    }

    // setupAdvancedRoutes() {

    //     try {
    //         const authRoutes = require('./routes/auth');
    //         if (authRoutes) {
    //             this.app.use('/api/auth', authRoutes);
    //             logger.info('Auth routes mounted');
    //         }
    //     } catch (e) {
    //         logger.error('Could not load auth routes:', e.message);
    //     }

    //     try {
    //         const fileRoutes = require('./routes/files');
    //         if (fileRoutes) {
    //             this.app.use('/api/files', fileRoutes);
    //             this.app.use('/download', fileRoutes);      // Legacy support
    //             this.app.use('/upload', fileRoutes);        // Android compatibility
    //             logger.info('File routes mounted at multiple endpoints');
    //         }
    //     } catch (e) {
    //         logger.warn('Could not load file routes:', e.message);
    //     }


    //     try {
    //         logger.info('Attempting to load auth routes from ./routes/auth');
    //         const authRoutes = require('./routes/auth');
    //         if (authRoutes) {
    //             this.app.use('/api/auth', authRoutes);
    //             logger.info('Auth routes mounted at /api/auth');
    //         }
    //     } catch (e) {
    //         logger.error('Could not load auth routes:', { message: e.message, stack: e.stack });
    //     }

    //     // In server.js, add this line in setupAdvancedRoutes():
    //     try {

    //         // logger.info('Attempting to load auth routes from ./routes/auth');

    //         const fileRoutes = require('./routes/files');
    //         this.app.use('/upload', fileRoutes);  // Add this line
    //         this.app.use('/download', require('./routes/files')); // This lets /download?file=... reach files router


    //         if (fileRoutes) {
    //             this.app.use('/upload', fileRoutes);  // Add this line

    //             this.app.use('/api/files', fileRoutes);  // Existing
    //             this.app.use('/api/upload', fileRoutes); // ADD THIS LINE - alias for upload
    //             this.app.use('/download', fileRoutes);
    //             logger.info('File/Upload routes loaded');

    //         }
    //     } catch (e) { logger.warn('Could not load file routes:', e.message || e); }

    //     try {
    //         logger.info('Attempting to load auth routes from ./routes/auth');
    //         const authRoutes = require('./routes/auth');
    //         if (authRoutes) { this.app.use('/api/auth', authRoutes); logger.info('Auth routes mounted at /api/auth'); }
    //         else logger.warn('Auth routes module returned empty export');
    //     } catch (e) {
    //         logger.error('Could not load auth routes:', { message: e.message, stack: e.stack });
    //     }

    //     try {
    //         const fileRoutes = require('./routes/files');
    //         if (fileRoutes) {
    //             this.app.use('/api/files', fileRoutes);
    //             this.app.use('/download', fileRoutes);
    //             logger.info('File routes loaded at /api/files and /download');
    //         }
    //     } catch (e) {
    //         logger.warn('Could not load file routes:', e.message || e);
    //     }

    //     try {
    //         const syncRoutes = require('./routes/sync');
    //         if (syncRoutes) {
    //             this.app.use('/api/sync', syncRoutes);
    //             logger.info('Sync routes loaded');
    //         }
    //     } catch (e) {
    //         logger.warn('Could not load sync routes:', e.message || e);
    //     }

    //     try {
    //         const fileRoutes = require('./routes/files');
    //         if (fileRoutes) {
    //             this.app.use('/api/files', fileRoutes);
    //             this.app.use('/download', fileRoutes);      // Legacy support
    //             this.app.use('/upload', fileRoutes);        // Android compatibility
    //             logger.info('File routes mounted at multiple endpoints');
    //         }
    //     } catch (e) {
    //         logger.warn('Could not load file routes:', e.message);
    //     }

    // }

    setupAdvancedRoutes() {
        try {
            logger.info('Setting up advanced routes...');

            // Load auth routes
            try {
                logger.info('Loading auth routes...');
                const authRoutes = require('./routes/auth');
                this.app.use('/api/auth', authRoutes);
                logger.info('✅ Auth routes loaded successfully at /api/auth');
            } catch (error) {
                logger.error('❌ Failed to load auth routes:', {
                    message: error.message,
                    stack: error.stack
                });
            }

            // Load file routes
            try {
                logger.info('Loading file routes...');
                const fileRoutes = require('./routes/files');
                this.app.use('/api/files', fileRoutes);
                logger.info('✅ File routes loaded successfully at /api/files');
            } catch (error) {
                logger.error('❌ Failed to load file routes:', {
                    message: error.message,
                    stack: error.stack
                });
            }

            // Load sync routes (these seem to work)
            try {
                const syncRoutes = require('./routes/sync');
                this.app.use('/api/sync', syncRoutes);
                logger.info('✅ Sync routes loaded successfully');
            } catch (error) {
                logger.warn('Could not load sync routes:', error.message);
            }

            logger.info('Advanced routes setup completed');

        } catch (error) {
            logger.error('Failed to setup advanced routes:', error);
        }
    }

    async shutdown(signal) {
        if (this.isShuttingDown) return;
        this.isShuttingDown = true;
        logger.info(`Received ${signal}. Starting graceful shutdown...`);
        const shutdownTimeout = setTimeout(() => { logger.error('Graceful shutdown timeout. Forcing exit.'); process.exit(1); }, 10000);

        try {
            if (this.server) { await new Promise(resolve => this.server.close(resolve)); logger.info('HTTP server closed'); }
            if (this.io) { try { this.io.close(); logger.info('WebSocket server closed'); } catch (e) { logger.warn('Error closing WebSocket server', e.message); } }
            clearTimeout(shutdownTimeout);
            logger.info('Graceful shutdown completed');
            process.exit(0);
        } catch (e) {
            logger.error('Error during shutdown:', e);
            clearTimeout(shutdownTimeout);
            process.exit(1);
        }
    }
}

const server = new CloudBackupServer();
if (require.main === module) server.start();
module.exports = server;
