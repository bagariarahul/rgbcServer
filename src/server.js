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
const path = require('path'); // FIX: Added path module

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
        this.server = null;
        this.PORT = process.env.PORT || 3000;
        this.isShuttingDown = false;
        // FIX: Define root path for reliable file access
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
            cookie: { secure: false } // Set to true if using HTTPS
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
        // FIX: Use absolute paths
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
            // 1. Connect Database (with retry logic handled in database.js)
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
        
        // Helper to safely load routes
        const loadRoute = (pathStr, requirePath) => {
            try {
                const routeModule = require(requirePath);
                // FIX: Check if module is a function (Router) before using
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

        loadRoute('/api/auth', './routes/auth');
        loadRoute('/api/files', './routes/files');
        loadRoute('/api/sync', './routes/sync');
        
        // Android/Legacy aliases
        loadRoute('/upload', './routes/files');
        loadRoute('/download', './routes/files');
    }
}

const server = new CloudBackupServer();
if (require.main === module) server.start();
module.exports = server;