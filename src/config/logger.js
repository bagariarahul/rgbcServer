const winston = require('winston');
const path = require('path');
const fs = require('fs');

// Ensure logs directory exists
const logsDir = path.join(process.cwd(), 'logs');
if (!fs.existsSync(logsDir)) {
    fs.mkdirSync(logsDir, { recursive: true });
}

// Define log levels
const logLevels = {
    error: 0,
    warn: 1,
    info: 2,
    audit: 3,
    auth: 4,
    queue: 5,
    debug: 6
};

// Define log colors
const logColors = {
    error: 'red',
    warn: 'yellow',
    info: 'green',
    audit: 'blue',
    auth: 'cyan',
    queue: 'magenta',
    debug: 'white'
};

winston.addColors(logColors);

// Custom format for console output
const consoleFormat = winston.format.combine(
    winston.format.timestamp({ format: 'HH:mm:ss' }),
    winston.format.colorize({ all: true }),
    winston.format.printf(({ timestamp, level, message, service, ...meta }) => {
        let metaStr = '';
        if (Object.keys(meta).length > 0) {
            metaStr = ' ' + JSON.stringify(meta);
        }
        return `${timestamp} ${level}: ${message}${metaStr}`;
    })
);

// Custom format for file output
const fileFormat = winston.format.combine(
    winston.format.timestamp(),
    winston.format.errors({ stack: true }),
    winston.format.json(),
    winston.format.printf((info) => {
        // Add service name and version to all log entries
        info.service = 'cloudbackup-server';
        info.version = '1.0.0';
        return JSON.stringify(info);
    })
);

// Create transports array
const transports = [
    // Console transport
    new winston.transports.Console({
        format: consoleFormat,
        level: process.env.LOG_LEVEL || 'info'
    }),

    // File transport for all logs
    new winston.transports.File({
        filename: path.join(logsDir, 'app.log'),
        format: fileFormat,
        maxsize: 10 * 1024 * 1024, // 10MB
        maxFiles: 5,
        tailable: true
    }),

    // File transport for errors only
    new winston.transports.File({
        filename: path.join(logsDir, 'error.log'),
        format: fileFormat,
        level: 'error',
        maxsize: 10 * 1024 * 1024, // 10MB
        maxFiles: 5,
        tailable: true
    }),

    // File transport for audit logs
    new winston.transports.File({
        filename: path.join(logsDir, 'audit.log'),
        format: fileFormat,
        level: 'audit',
        maxsize: 10 * 1024 * 1024, // 10MB
        maxFiles: 10,
        tailable: true
    })
];

// Create logger instance
const logger = winston.createLogger({
    levels: logLevels,
    level: process.env.LOG_LEVEL || 'info',
    format: fileFormat,
    transports: transports,
    exitOnError: false,
    exceptionHandlers: [
        new winston.transports.File({
            filename: path.join(logsDir, 'exceptions.log'),
            format: fileFormat,
            maxsize: 10 * 1024 * 1024,
            maxFiles: 3
        })
    ],
    rejectionHandlers: [
        new winston.transports.File({
            filename: path.join(logsDir, 'rejections.log'),
            format: fileFormat,
            maxsize: 10 * 1024 * 1024,
            maxFiles: 3
        })
    ]
});

// Add custom logging methods
logger.audit = (event, data = {}, req = null) => {
    const auditLog = {
        event,
        data,
        timestamp: new Date().toISOString(),
        level: 'audit'
    };

    if (req) {
        auditLog.request = {
            ip: req.ip || req.connection?.remoteAddress,
            userAgent: req.get('User-Agent'),
            method: req.method,
            url: req.originalUrl,
            requestId: req.requestId
        };
    }

    logger.log('audit', 'Audit event', auditLog);
};

logger.auth = (action, userId, data = {}, req = null) => {
    const authLog = {
        action,
        userId,
        data,
        timestamp: new Date().toISOString(),
        level: 'auth'
    };

    if (req) {
        authLog.request = {
            ip: req.ip || req.connection?.remoteAddress,
            userAgent: req.get('User-Agent'),
            requestId: req.requestId
        };
    }

    logger.log('auth', `Auth: ${action}`, authLog);
};

logger.queue = (jobType, jobId, status, data = {}) => {
    const queueLog = {
        jobType,
        jobId,
        status,
        data,
        timestamp: new Date().toISOString(),
        level: 'queue'
    };

    logger.log('queue', `Queue: ${jobType} ${status}`, queueLog);
};

// Stream interface for morgan HTTP request logging
logger.stream = {
    write: (message) => {
        logger.info(message.trim());
    }
};

// Add request context to all logs when available
const originalLog = logger.log;
logger.log = function(level, message, meta = {}) {
    // Add common fields to all logs
    const enhancedMeta = {
        ...meta,
        pid: process.pid,
        hostname: require('os').hostname(),
        timestamp: new Date().toISOString()
    };

    return originalLog.call(this, level, message, enhancedMeta);
};

// Handle uncaught exceptions
process.on('uncaughtException', (error) => {
    logger.error('Uncaught Exception:', {
        error: {
            message: error.message,
            stack: error.stack,
            code: error.code
        },
        process: {
            pid: process.pid,
            uid: process.getuid ? process.getuid() : null,
            gid: process.getgid ? process.getgid() : null,
            cwd: process.cwd(),
            execPath: process.execPath,
            version: process.version,
            argv: process.argv,
            memoryUsage: process.memoryUsage()
        },
        os: {
            loadavg: require('os').loadavg(),
            uptime: require('os').uptime()
        },
        trace: error.stack ? error.stack.split('\n').map(line => {
            const match = line.match(/at\s+(.+?)\s+\((.+?):(\d+):(\d+)\)/);
            if (match) {
                return {
                    function: match[1],
                    file: match[2],
                    line: parseInt(match[3]),
                    column: parseInt(match[4]),
                    native: match[2].startsWith('node:')
                };
            }
            return line.trim();
        }) : []
    });
});

// Handle unhandled promise rejections
process.on('unhandledRejection', (reason, promise) => {
    logger.error('Unhandled Rejection:', {
        reason: reason instanceof Error ? {
            message: reason.message,
            stack: reason.stack
        } : reason,
        promise: promise.toString()
    });
});

module.exports = logger;