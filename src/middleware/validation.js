const { body, validationResult } = require('express-validator');

/**
 * Validation middleware for different request types
 */

// User registration validation
const validateRegistration = [
    body('email')
        .isEmail()
        .normalizeEmail()
        .withMessage('Valid email is required'),
    body('password')
        .isLength({ min: 8 })
        .matches(/^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]/)
        .withMessage('Password must be at least 8 characters with uppercase, lowercase, number, and special character'),
    body('firstName')
        .isLength({ min: 1, max: 100 })
        .trim()
        .escape()
        .withMessage('First name is required'),
    body('lastName')
        .isLength({ min: 1, max: 100 })
        .trim()
        .escape()
        .withMessage('Last name is required'),
    body('deviceName')
        .isLength({ min: 1, max: 100 })
        .trim()
        .escape()
        .withMessage('Device name is required'),
    body('deviceType')
        .isIn(['ANDROID', 'IOS', 'WEB'])
        .withMessage('Valid device type is required'),
    body('deviceId')
        .isUUID()
        .withMessage('Valid device ID is required')
];

// User login validation
const validateLogin = [
    body('email')
        .isEmail()
        .normalizeEmail()
        .withMessage('Valid email is required'),
    body('password')
        .isLength({ min: 1 })
        .withMessage('Password is required'),
    body('deviceName')
        .optional()
        .isLength({ min: 1, max: 100 })
        .trim()
        .escape(),
    body('deviceType')
        .isIn(['ANDROID', 'IOS', 'WEB'])
        .withMessage('Valid device type is required'),
    body('deviceId')
        .isUUID()
        .withMessage('Valid device ID is required')
];

// File upload validation
const validateFileUpload = [
    body('fileName')
        .isLength({ min: 1, max: 255 })
        .withMessage('File name is required'),
    body('fileSize')
        .isInt({ min: 1 })
        .withMessage('Valid file size is required'),
    body('fileHash')
        .optional()
        .isLength({ min: 64, max: 64 })
        .withMessage('File hash must be 64 characters'),
    body('mimeType')
        .optional()
        .isLength({ min: 1, max: 100 })
        .withMessage('Valid MIME type required')
];

// Chunk upload validation
const validateChunkUpload = [
    body('fileId')
        .isUUID()
        .withMessage('Valid file ID is required'),
    body('chunkNumber')
        .isInt({ min: 0 })
        .withMessage('Valid chunk number is required'),
    body('totalChunks')
        .isInt({ min: 1 })
        .withMessage('Valid total chunks count is required'),
    body('fileName')
        .isLength({ min: 1, max: 255 })
        .withMessage('File name is required'),
    body('chunkHash')
        .optional()
        .isLength({ min: 64, max: 64 })
        .withMessage('Chunk hash must be 64 characters')
];

// Validation error handler middleware
const handleValidationErrors = (req, res, next) => {
    const errors = validationResult(req);
    
    if (!errors.isEmpty()) {
        return res.status(400).json({
            error: 'Validation failed',
            message: 'Please check your input data',
            code: 'VALIDATION_ERROR',
            details: errors.array().map(error => ({
                field: error.path,
                message: error.msg,
                value: error.value
            }))
        });
    }
    
    next();
};

module.exports = {
    validateRegistration,
    validateLogin,
    validateFileUpload,
    validateChunkUpload,
    handleValidationErrors
};