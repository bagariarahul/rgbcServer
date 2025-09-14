const logger = require('../config/logger');

/**
 * Global error handler middleware
 * Handles all unhandled errors in the application
 */
const errorHandler = (err, req, res, next) => {
    // Log the error
    logger.error('Global error handler:', {
        error: err.message,
        stack: err.stack,
        url: req.originalUrl,
        method: req.method,
        ip: req.ip,
        userAgent: req.get('User-Agent'),
        userId: req.user?.id,
        requestId: req.requestId
    });

    // Default error
    let error = {
        status: 500,
        error: 'Internal Server Error',
        message: 'Something went wrong on our end',
        code: 'INTERNAL_ERROR'
    };

    // Handle different error types
    if (err.name === 'ValidationError') {
        error = {
            status: 400,
            error: 'Validation Error',
            message: err.message,
            code: 'VALIDATION_ERROR',
            details: err.errors || err.details
        };
    } else if (err.name === 'SequelizeValidationError') {
        error = {
            status: 400,
            error: 'Database Validation Error',
            message: 'Data validation failed',
            code: 'DATABASE_VALIDATION_ERROR',
            details: err.errors?.map(e => ({
                field: e.path,
                message: e.message,
                value: e.value
            }))
        };
    } else if (err.name === 'SequelizeUniqueConstraintError') {
        error = {
            status: 409,
            error: 'Duplicate Entry',
            message: 'A record with this data already exists',
            code: 'DUPLICATE_ENTRY',
            details: err.errors?.map(e => ({
                field: e.path,
                message: e.message
            }))
        };
    } else if (err.name === 'SequelizeForeignKeyConstraintError') {
        error = {
            status: 400,
            error: 'Foreign Key Constraint Error',
            message: 'Referenced record does not exist',
            code: 'FOREIGN_KEY_ERROR'
        };
    } else if (err.name === 'JsonWebTokenError') {
        error = {
            status: 401,
            error: 'Invalid Token',
            message: 'Authentication token is invalid',
            code: 'INVALID_TOKEN'
        };
    } else if (err.name === 'TokenExpiredError') {
        error = {
            status: 401,
            error: 'Token Expired',
            message: 'Authentication token has expired',
            code: 'TOKEN_EXPIRED'
        };
    } else if (err.name === 'MulterError') {
        if (err.code === 'LIMIT_FILE_SIZE') {
            error = {
                status: 413,
                error: 'File Too Large',
                message: 'Uploaded file exceeds size limit',
                code: 'FILE_SIZE_LIMIT'
            };
        } else if (err.code === 'LIMIT_FILE_COUNT') {
            error = {
                status: 400,
                error: 'Too Many Files',
                message: 'Too many files uploaded',
                code: 'FILE_COUNT_LIMIT'
            };
        } else {
            error = {
                status: 400,
                error: 'Upload Error',
                message: err.message,
                code: 'UPLOAD_ERROR'
            };
        }
    } else if (err.code === 'ENOENT') {
        error = {
            status: 404,
            error: 'File Not Found',
            message: 'Requested file does not exist',
            code: 'FILE_NOT_FOUND'
        };
    } else if (err.code === 'EACCES') {
        error = {
            status: 403,
            error: 'Access Denied',
            message: 'Insufficient permissions to access file',
            code: 'ACCESS_DENIED'
        };
    } else if (err.code === 'ENOSPC') {
        error = {
            status: 507,
            error: 'Insufficient Storage',
            message: 'Not enough storage space available',
            code: 'STORAGE_FULL'
        };
    }

    // Handle custom error codes
    if (err.status || err.statusCode) {
        error.status = err.status || err.statusCode;
    }

    if (err.code) {
        error.code = err.code;
    }

    // Don't expose sensitive information in production
    if (process.env.NODE_ENV === 'production') {
        delete error.details;
        if (error.status >= 500) {
            error.message = 'An internal server error occurred';
        }
    } else {
        // Include stack trace in development
        error.stack = err.stack;
    }

    // Add request ID for tracing
    error.requestId = req.requestId;
    error.timestamp = new Date().toISOString();

    // Send error response
    res.status(error.status).json(error);
};

/**
 * 404 handler for unmatched routes
 */
const notFoundHandler = (req, res) => {
    const error = {
        status: 404,
        error: 'Not Found',
        message: `Route ${req.method} ${req.originalUrl} not found`,
        code: 'ROUTE_NOT_FOUND',
        requestId: req.requestId,
        timestamp: new Date().toISOString()
    };

    logger.warn('Route not found:', {
        url: req.originalUrl,
        method: req.method,
        ip: req.ip,
        userAgent: req.get('User-Agent'),
        requestId: req.requestId
    });

    res.status(404).json(error);
};

module.exports = {
    errorHandler,
    notFoundHandler
};