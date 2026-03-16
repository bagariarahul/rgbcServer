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
const path = require('path');

// Use try-catch for logger to prevent startup crash if missing
let logger;
try {
    logger = require('./config/logger');
} catch (e) {
    console.error('Logger not found, falling back to console');
    logger = console;
}

class CloudBackupServer {
    constructor() {
        this.app = express();
        this.app.set('trust proxy', 1);
        this.server = null;
        this.PORT = process.env.PORT || 3000;
        this.isShuttingDown = false;
        this.rootPath = path.resolve(__dirname, '..');
    }

    async start() {
        try {
            logger.info('Starting Cloud Backup Server...');
            this.server = http.createServer(this.app);

            this.setupBasicMiddleware();
            this.setupBasicRoutes();
            this.setupErrorHandling();

            await this.createDirectories();

            // Start listening
            await new Promise((resolve, reject) => {
                this.server.listen(this.PORT, '0.0.0.0', (err) => {
                    if (err) return reject(err);
                    console.log(`✅ Server now listening on 0.0.0.0:${this.PORT}`);
                    resolve();
                });
            });

            // Initialize DB/Redis/Routes
            await this.initializeAdvancedFeatures();

        } catch (error) {
            logger.error('Failed to start server:', error);
            process.exit(1);
        }
    }

    setupBasicMiddleware() {
        this.app.use(helmet({ crossOriginEmbedderPolicy: false, contentSecurityPolicy: false }));
        this.app.use(cors({ origin: true, credentials: true }));
        this.app.use(compression());
        this.app.use(express.json({ limit: '50mb' }));
        this.app.use(express.urlencoded({ extended: true, limit: '50mb' }));

        this.app.use(rateLimit({
            windowMs: 15 * 60 * 1000,
            max: 1000,
            message: { error: 'Too many requests' }
        }));

        this.app.use(session({
            secret: process.env.SESSION_SECRET || 'dev_secret',
            resave: false,
            saveUninitialized: false,
            cookie: { secure: false }
        }));

        // Request logging
        if (process.env.NODE_ENV !== 'test') {
            this.app.use(morgan('dev'));
        }
    }

    setupBasicRoutes() {
        this.app.get('/health', (req, res) => res.json({ status: 'healthy', timestamp: new Date() }));
        this.app.get('/', (req, res) => res.json({ service: 'Cloud Backup API', status: 'running' }));
    }

    setupErrorHandling() {
        this.app.use((err, req, res, next) => {
            logger.error('Global error:', err.message);
            res.status(500).json({ error: 'Internal Server Error' });
        });
    }

    async createDirectories() {
        const dirs = [
            path.join(this.rootPath, 'storage', 'uploads'),
            path.join(this.rootPath, 'storage', 'temp'),
            path.join(this.rootPath, 'logs')
        ];
        
        for (const dir of dirs) {
            try { 
                await fs.mkdir(dir, { recursive: true }); 
            } catch (e) { 
                logger.warn(`Directory check failed for ${dir}: ${e.message}`); 
            }
        }
    }

    async initializeAdvancedFeatures() {
        try {
            // 1. Connect Database
            const dbModule = require('./config/database');
            await dbModule.connectDB();

            // 2. Connect Redis
            try {
                const redisModule = require('./config/redis');
                if (typeof redisModule.connectRedis === 'function') {
                    await redisModule.connectRedis();
                }
            } catch (e) {
                logger.warn('Redis connection failed (continuing without cache):', e.message);
            }

            // 3. Load Routes
            this.setupAdvancedRoutes();

        } catch (e) {
            logger.error('Advanced initialization failed:', e);
        }
    }

    setupAdvancedRoutes() {
        logger.info('Mounting API Routes...');

        // ── JWT / M2M API Key verification middleware ────────────────
        let verifyToken;
        try {
            verifyToken = require('./middleware/verifyToken');
            logger.info('✅ JWT verification middleware loaded');
        } catch (e) {
            logger.warn('❌ verifyToken middleware not found, routes will be unprotected');
            verifyToken = (req, res, next) => next();
        }
        
        // Helper to safely load routes
        const loadRoute = (pathStr, requirePath) => {
            try {
                const routeModule = require(requirePath);
                if (typeof routeModule === 'function') {
                    this.app.use(pathStr, routeModule);
                    logger.info(`✅ Mounted ${pathStr}`);
                } else {
                    logger.warn(`❌ Module ${requirePath} is not a valid Router middleware`);
                }
            } catch (e) {
                logger.warn(`❌ Failed to load ${pathStr}: ${e.message}`);
            }
        };

        // ── Public routes (no auth required) ────────────────────────
        loadRoute('/api/auth', './routes/auth');
        loadRoute('/api/auth', './routes/googleAuth');

        // ── Protected routes (JWT or M2M API Key required) ──────────
        this.app.use('/api/files', verifyToken);
        this.app.use('/api/sync', verifyToken);
        this.app.use('/api/server-info', verifyToken);
        this.app.use('/api/devices', verifyToken);     // Sprint 2: P2P signaling

        loadRoute('/api/files', './routes/files');
        loadRoute('/api/sync', './routes/sync');
        loadRoute('/api/server-info', './routes/serverInfo');
        loadRoute('/api/devices', './routes/devices');  // Sprint 2: P2P signaling

        // Android/Legacy aliases (also protected)
        this.app.use('/upload', verifyToken);
        this.app.use('/download', verifyToken);
        loadRoute('/upload', './routes/files');
        loadRoute('/download', './routes/files');
    }
}

const server = new CloudBackupServer();
if (require.main === module) server.start();
module.exports = server;